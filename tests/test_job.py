"""The job's entry point: which store it uses and what its exit code means.

The exit code is an interface. A monitoring alert fires on any failed
execution, so the code must be non-zero exactly when the owner needs to act,
and zero whenever the published document is truthful -- including when every
upstream is down and the document says so.
"""

from __future__ import annotations

import json
import logging
import urllib.error

import pytest

from orbital_watch import job
from orbital_watch.config import (
    BAKED_DOCUMENT_PATH,
    DOCUMENT_KEY,
    LOCAL_LIVE_DOCUMENT_PATH,
    PUBLIC_BUCKET_ENV,
    STATE_BUCKET_ENV,
)
from orbital_watch.store import LocalStore, StoreError
from tests.helpers import ALL_SOURCES_OK, responder


@pytest.fixture(autouse=True)
def no_buckets(monkeypatch):
    """Whatever the developer's shell exports, tests run against directories."""
    monkeypatch.delenv(STATE_BUCKET_ENV, raising=False)
    monkeypatch.delenv(PUBLIC_BUCKET_ENV, raising=False)


def healthy(monkeypatch) -> None:
    monkeypatch.setattr("urllib.request.urlopen", responder(ALL_SOURCES_OK))


def blackout(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        responder({}, default=urllib.error.URLError("network is unreachable")),
    )


def run_job(tmp_path, *extra) -> int:
    return job.main(
        ["--local-dir", str(tmp_path / "local"), "--out", str(tmp_path / "dist"), *extra]
    )


def published(tmp_path) -> dict:
    return json.loads((tmp_path / "local" / "public" / DOCUMENT_KEY).read_text())


# --------------------------------------------------------------------------
# Exit codes
# --------------------------------------------------------------------------


def test_healthy_run_exits_zero_and_publishes(tmp_path, monkeypatch):
    healthy(monkeypatch)

    assert run_job(tmp_path) == job.EXIT_OK
    assert published(tmp_path)["health"]["overall"] == "fresh"


def test_total_upstream_blackout_still_exits_zero(tmp_path, monkeypatch):
    """Upstream being down is not a failed run.

    The document says everything failed, which is true, and it is published.
    If this exited non-zero the failure alert would fire for an event the
    page already reports honestly, and would stop meaning "the pipeline is
    broken".
    """
    blackout(monkeypatch)

    assert run_job(tmp_path) == job.EXIT_OK
    document = published(tmp_path)
    assert document["health"]["overall"] == "failed"
    assert document["health"]["counts"]["failed"] == 7


def test_run_that_cannot_publish_exits_non_zero(tmp_path, monkeypatch):
    """The one thing that must fail the run: readers got nothing new."""
    healthy(monkeypatch)

    def refuse(self, *args, **kwargs):
        raise StoreError("backend unavailable")

    state = LocalStore(tmp_path / "state")
    public = LocalStore(tmp_path / "public")
    monkeypatch.setattr(public, "write", refuse.__get__(public))

    assert job.run(state, public) == job.EXIT_NOT_PUBLISHED
    assert public.read(DOCUMENT_KEY) is None


def test_malformed_document_exits_non_zero_and_keeps_the_previous_one(
    tmp_path, monkeypatch, caplog
):
    healthy(monkeypatch)
    state = LocalStore(tmp_path / "state")
    public = LocalStore(tmp_path / "public")
    assert job.run(state, public) == job.EXIT_OK
    before = public.read(DOCUMENT_KEY)

    real_build = job.build_document

    def corrupting_build(*args, **kwargs):
        document = real_build(*args, **kwargs)
        document["health"]["sources"][0]["state"] = "fresh"
        document["health"]["sources"][0]["outcome"] = "cache_fallback"
        return document

    monkeypatch.setattr(job, "build_document", corrupting_build)
    with caplog.at_level(logging.ERROR):
        code = job.run(state, public)

    assert code == job.EXIT_MALFORMED
    assert public.read(DOCUMENT_KEY) == before
    assert "NOT published" in caplog.text
    assert "a fallback is never fresh" in caplog.text


def test_bug_in_our_own_code_exits_non_zero(tmp_path, monkeypatch):
    def bug(*args, **kwargs):
        raise RuntimeError("a bug")

    monkeypatch.setattr(job, "build_document", bug)

    code = job.run(LocalStore(tmp_path / "state"), LocalStore(tmp_path / "public"))

    assert code == job.EXIT_INTERNAL_ERROR


def test_superseded_run_exits_zero(tmp_path, monkeypatch):
    """Deferring to a newer document from an overlapping run is a success."""
    healthy(monkeypatch)
    monkeypatch.setattr(job, "publish", lambda *args, **kwargs: "superseded")

    code = job.run(LocalStore(tmp_path / "state"), LocalStore(tmp_path / "public"))

    assert code == job.EXIT_OK


# --------------------------------------------------------------------------
# Local runs
# --------------------------------------------------------------------------


def test_local_run_writes_the_site_next_to_the_document(tmp_path, monkeypatch):
    """One command, no cloud: a directory a plain web server can serve."""
    healthy(monkeypatch)

    assert run_job(tmp_path) == job.EXIT_OK

    dist = tmp_path / "dist"
    document = published(tmp_path)
    assert (dist / "index.html").is_file()
    assert (dist / "assets" / "app.js").is_file()
    assert json.loads((dist / BAKED_DOCUMENT_PATH).read_text()) == document
    assert json.loads((dist / LOCAL_LIVE_DOCUMENT_PATH).read_text()) == document
    assert LOCAL_LIVE_DOCUMENT_PATH in (dist / "assets" / "config.js").read_text()


def test_local_run_can_skip_the_site(tmp_path, monkeypatch):
    healthy(monkeypatch)

    assert run_job(tmp_path, "--no-site") == job.EXIT_OK
    assert published(tmp_path)
    assert not (tmp_path / "dist").exists()


def test_second_local_run_reuses_state_and_spares_celestrak(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr("urllib.request.urlopen", responder(ALL_SOURCES_OK, calls=calls))

    assert run_job(tmp_path, "--no-site") == job.EXIT_OK
    assert run_job(tmp_path, "--no-site") == job.EXIT_OK

    celestrak_requests = [r.full_url for r in calls if "celestrak" in r.full_url]
    assert len(celestrak_requests) == 3, "the second run must not ask Celestrak again"
    assert published(tmp_path)["health"]["overall"] == "fresh"


# --------------------------------------------------------------------------
# Choosing the store
# --------------------------------------------------------------------------


def test_no_bucket_variables_means_local_directories(tmp_path):
    state, public, local = job.stores_from_environment(tmp_path)

    assert local is True
    assert isinstance(state, LocalStore)
    assert isinstance(public, LocalStore)
    assert state.describe() != public.describe()


@pytest.mark.parametrize("only", [STATE_BUCKET_ENV, PUBLIC_BUCKET_ENV])
def test_one_bucket_variable_without_the_other_is_refused(tmp_path, monkeypatch, only):
    """Half a cloud configuration is a mistake, not a mode."""
    monkeypatch.setenv(only, "some-bucket")

    with pytest.raises(ValueError, match="both"):
        job.stores_from_environment(tmp_path)
    assert job.main(["--local-dir", str(tmp_path)]) == job.EXIT_NOT_PUBLISHED


def test_both_bucket_variables_select_cloud_storage(tmp_path, monkeypatch):
    monkeypatch.setenv(STATE_BUCKET_ENV, "state-bucket")
    monkeypatch.setenv(PUBLIC_BUCKET_ENV, "public-bucket")
    built = []

    class RecordingStore:
        def __init__(self, bucket_name):
            built.append(bucket_name)

    monkeypatch.setattr(job, "GcsStore", RecordingStore)
    _state, _public, local = job.stores_from_environment(tmp_path)

    assert local is False
    assert built == ["state-bucket", "public-bucket"]


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------


def test_cloud_run_logs_are_json_with_a_severity(monkeypatch, capsys):
    """On Cloud Run each line is an object Cloud Logging reads a severity from."""
    monkeypatch.setenv("CLOUD_RUN_JOB", "orbital-watch-refresh")
    job.configure_logging()

    logging.getLogger("orbital_watch.test").warning("source=%s degraded", "swpc_kp")

    line = capsys.readouterr().err.strip().splitlines()[-1]
    assert json.loads(line) == {
        "severity": "WARNING",
        "message": "source=swpc_kp degraded",
        "logger": "orbital_watch.test",
    }
    logging.basicConfig(force=True)  # leave no handler bound to a captured stream
