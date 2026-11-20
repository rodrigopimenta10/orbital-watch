"""Assembling the static site around a baked copy of the document."""

from __future__ import annotations

import json

import pytest

from orbital_watch import site
from orbital_watch.config import BAKED_DOCUMENT_PATH, LOCAL_LIVE_DOCUMENT_PATH
from orbital_watch.document import serialize
from orbital_watch.pipeline import build_document
from tests.helpers import ALL_SOURCES_OK, FakeClock, responder

LIVE_URL = "https://storage.googleapis.com/example-public-bucket/latest.json"


@pytest.fixture
def document_bytes(state_store, monkeypatch) -> bytes:
    monkeypatch.setattr("urllib.request.urlopen", responder(ALL_SOURCES_OK))
    return serialize(build_document(state_store, clock=FakeClock()))


@pytest.fixture
def web_dir(tmp_path):
    """A stand-in frontend, so these tests do not depend on the real one."""
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><title>test</title>")
    (web / "assets" / "app.js").write_text("/* app */")
    (web / "assets" / "config.js").write_text("/* placeholder committed in web/ */")
    return web


def test_site_is_the_frontend_plus_the_baked_document_and_config(
    tmp_path, web_dir, document_bytes
):
    out = tmp_path / "dist"

    site.build_site(
        document=document_bytes, live_url=LIVE_URL, out_dir=out, web_dir=web_dir
    )

    assert (out / "index.html").read_text() == (web_dir / "index.html").read_text()
    assert (out / "assets" / "app.js").is_file()
    assert (out / BAKED_DOCUMENT_PATH).read_bytes() == document_bytes
    assert not (out / LOCAL_LIVE_DOCUMENT_PATH).exists(), (
        "deploys carry no local stand-in"
    )
    config = (out / "assets" / "config.js").read_text()
    assert LIVE_URL in config
    assert BAKED_DOCUMENT_PATH in config
    assert "placeholder" not in config, "the generated config must replace web/'s"


def test_config_values_cannot_break_out_of_their_string():
    """Whatever is passed as the address reaches the browser as a string."""
    hostile = '";alert(1);//</script>'

    rendered = site.render_config(hostile)

    payload = rendered.split("=", 1)[1].rstrip().rstrip(";")
    assert json.loads(payload)["liveDocumentUrl"] == hostile


def test_local_build_also_places_a_live_copy(tmp_path, web_dir, document_bytes):
    out = tmp_path / "dist"

    site.build_site(
        document=document_bytes, out_dir=out, web_dir=web_dir, local_live_copy=True
    )

    assert (out / LOCAL_LIVE_DOCUMENT_PATH).read_bytes() == document_bytes
    assert LOCAL_LIVE_DOCUMENT_PATH in (out / "assets" / "config.js").read_text()


def test_malformed_document_is_not_baked_and_the_old_site_survives(
    tmp_path, web_dir, document_bytes
):
    """A deploy must not ship a fallback copy the page cannot render."""
    out = tmp_path / "dist"
    site.build_site(
        document=document_bytes, live_url=LIVE_URL, out_dir=out, web_dir=web_dir
    )
    before = (out / BAKED_DOCUMENT_PATH).read_bytes()

    broken = json.loads(document_bytes)
    del broken["health"]
    for bad in (json.dumps(broken).encode(), b"<html>an error page</html>", b""):
        with pytest.raises(ValueError):
            site.build_site(document=bad, live_url=LIVE_URL, out_dir=out, web_dir=web_dir)

    assert (out / BAKED_DOCUMENT_PATH).read_bytes() == before
    assert not (tmp_path / "dist.staging").exists()


def test_missing_frontend_fails_and_leaves_the_old_site(
    tmp_path, web_dir, document_bytes
):
    out = tmp_path / "dist"
    site.build_site(
        document=document_bytes, live_url=LIVE_URL, out_dir=out, web_dir=web_dir
    )

    with pytest.raises(OSError):
        site.build_site(
            document=document_bytes,
            live_url=LIVE_URL,
            out_dir=out,
            web_dir=tmp_path / "no-such-directory",
        )

    assert (out / "index.html").is_file()


def test_rebuild_replaces_the_previous_site_completely(tmp_path, web_dir, document_bytes):
    """Files from an earlier build must not linger in the new one."""
    out = tmp_path / "dist"
    site.build_site(
        document=document_bytes, live_url=LIVE_URL, out_dir=out, web_dir=web_dir
    )
    (out / "leftover-from-an-old-build.txt").write_text("stale")

    site.build_site(
        document=document_bytes, live_url=LIVE_URL, out_dir=out, web_dir=web_dir
    )

    assert not (out / "leftover-from-an-old-build.txt").exists()
    assert not (tmp_path / "dist.staging").exists()
    assert not (tmp_path / "dist.previous").exists()


def test_debris_from_an_interrupted_build_does_not_block_the_next(
    tmp_path, web_dir, document_bytes
):
    """Staging and retired names may be occupied by a stray *file*."""
    out = tmp_path / "dist"
    (tmp_path / "dist.staging").write_text("a file where a directory should be")
    (tmp_path / "dist.previous").write_text("likewise")

    site.build_site(
        document=document_bytes, live_url=LIVE_URL, out_dir=out, web_dir=web_dir
    )

    assert (out / "index.html").is_file()


def test_command_line_builds_the_real_frontend_from_a_file(tmp_path, document_bytes):
    source = tmp_path / "latest.json"
    source.write_bytes(document_bytes)
    out = tmp_path / "dist"

    code = site.main(
        ["--document", str(source), "--live-url", LIVE_URL, "--out", str(out)]
    )

    assert code == 0
    assert (out / BAKED_DOCUMENT_PATH).read_bytes() == document_bytes
    assert (out / "index.html").is_file()
    assert LIVE_URL in (out / "assets" / "config.js").read_text()


def test_command_line_reports_failure(tmp_path):
    missing = tmp_path / "no-such-document.json"

    code = site.main(
        ["--document", str(missing), "--live-url", LIVE_URL, "--out", str(tmp_path / "d")]
    )

    assert code == 1
