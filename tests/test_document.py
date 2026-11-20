"""The gate in front of publication, and publication itself.

A document is checked twice over before readers can see it: for the shape the
page depends on, and for the honesty rules the README promises. A document
that fails either is withheld and the previous one stays up. Publication is
one conditional object write, so readers never see a mixture and an older run
never replaces a newer document.
"""

from __future__ import annotations

import copy
import json
from datetime import timedelta

import pytest

from orbital_watch.config import DOCUMENT_KEY
from orbital_watch.document import (
    DOCUMENT_CACHE_CONTROL,
    DOCUMENT_KIND,
    MalformedDocument,
    publish,
    serialize,
    validate,
)
from orbital_watch.pipeline import SOURCE_LABELS, build_document
from orbital_watch.store import PreconditionFailed, StoreError
from tests.helpers import ALL_SOURCES_OK, FakeClock, responder


@pytest.fixture
def document(state_store, monkeypatch) -> dict:
    """A real document, produced by the real pipeline from the fixtures."""
    monkeypatch.setattr("urllib.request.urlopen", responder(ALL_SOURCES_OK))
    return build_document(state_store, clock=FakeClock())


def check(document) -> list[str]:
    return validate(document, expected_sources=SOURCE_LABELS)


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def test_a_real_document_passes(document):
    assert check(document) == []
    assert document["kind"] == DOCUMENT_KIND


@pytest.mark.parametrize("not_a_document", [None, [], "text", 42])
def test_non_objects_are_rejected(not_a_document):
    assert check(not_a_document) == ["document is not an object"]


@pytest.mark.parametrize("section", ["meta", "sky", "passes", "space_weather", "health"])
def test_every_section_is_required(document, section):
    del document[section]
    assert any(section in problem for problem in check(document))


def _set(path: list, value):
    """Build a mutation that sets ``document[path...] = value``."""

    def mutate(document: dict) -> None:
        target = document
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _drop(path: list):
    def mutate(document: dict) -> None:
        target = document
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return mutate


MALFORMATIONS = {
    "wrong kind": _set(["kind"], "something-else"),
    "unknown schema version": _set(["schema_version"], 1),
    "generation time missing": _drop(["generated_at"]),
    "generation time without a zone": _set(["generated_at"], "2026-08-06T15:00:00"),
    "no cadence": _drop(["meta", "pipeline", "cadence_seconds"]),
    "zero silence limit": _set(["meta", "pipeline", "silence_limit_seconds"], 0),
    "observer latitude is text": _set(["meta", "observer", "latitude_deg"], "39.08"),
    "track start missing": _drop(["sky", "ephemeris", "start"]),
    "single-sample track": _set(["sky", "ephemeris", "samples"], 1),
    "satellites not a list": _set(["sky", "satellites"], {}),
    "track shorter than declared": _set(
        ["sky", "satellites", 0, "track"], [[0, 0, 1, 400]]
    ),
    "track sample is not numbers": _set(
        ["sky", "satellites", 0, "track", 3], [1.0, "east", 3.0, 400.0]
    ),
    "satellite without an epoch": _drop(["sky", "satellites", 0, "tle_epoch"]),
    "pass with a bad time": _set(["passes", "passes", 0, "start"], "soon"),
    "pass that ends before it starts": _set(
        ["passes", "passes", 0, "end"], "2020-01-01T00:00:00+00:00"
    ),
    "passes completeness missing": _drop(["passes", "complete"]),
    "weather available flag missing": _drop(["space_weather", "weather", "available"]),
    "available weather without a kp": _set(["space_weather", "weather", "kp"], None),
    "unknown severity": _set(["space_weather", "correlation", "severity"], "spicy"),
    "unknown source state": _set(["health", "sources", 0, "state"], "great"),
    "unknown source outcome": _set(["health", "sources", 0, "outcome"], "snapshot"),
    "thresholds missing": _drop(["health", "sources", 0, "thresholds"]),
    "last success is not a time": _set(["health", "sources", 0, "last_success"], "never"),
    "overall disagrees with the sources": _set(["health", "overall"], "failed"),
    "counts disagree with the sources": _set(["health", "counts", "fresh"], 3),
}


@pytest.mark.parametrize("name", sorted(MALFORMATIONS))
def test_malformed_documents_are_rejected(document, name):
    MALFORMATIONS[name](document)
    assert check(document), f"validation accepted a document with: {name}"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected_wherever_they_are(document, bad):
    """JSON has no NaN; a document carrying one cannot be read by a browser."""
    document["space_weather"]["weather"]["bz_nt"] = bad

    problems = check(document)

    assert any("non-finite" in p and "bz_nt" in p for p in problems)
    with pytest.raises(ValueError):
        serialize(document)


# --------------------------------------------------------------------------
# Honesty, checked on the finished document
# --------------------------------------------------------------------------


def test_a_dropped_source_is_rejected(document):
    """Every source must be accounted for; a missing row is not "healthy"."""
    document["health"]["sources"].pop()
    document["health"]["counts"]["fresh"] -= 1

    assert any("account for exactly" in p for p in check(document))


def test_an_unexpected_source_is_rejected(document):
    extra = copy.deepcopy(document["health"]["sources"][0])
    extra["name"] = "invented_source"
    document["health"]["sources"].append(extra)
    document["health"]["counts"]["fresh"] += 1

    assert any("account for exactly" in p for p in check(document))


def test_a_fallback_claiming_freshness_is_rejected(document):
    """The rule the whole project argues for, enforced on the output.

    Whatever a future change does to the classification code, a document in
    which a source that fell back to stored data is labelled fresh does not
    get published.
    """
    document["health"]["sources"][0]["outcome"] = "cache_fallback"

    assert any("a fallback is never fresh" in p for p in check(document))


def test_undated_data_claiming_freshness_is_rejected(document):
    document["health"]["sources"][0]["last_success"] = None

    assert any("fresh without a date" in p for p in check(document))


# --------------------------------------------------------------------------
# Publication
# --------------------------------------------------------------------------


def do_publish(document, store) -> str:
    return publish(document, store, expected_sources=SOURCE_LABELS)


def test_publish_writes_one_object_with_a_short_cache_lifetime(document, public_store):
    seen = {}
    real_write = public_store.write

    def spy(key, data, **kwargs):
        seen.update(key=key, **kwargs)
        return real_write(key, data, **kwargs)

    public_store.write = spy

    assert do_publish(document, public_store) == "published"
    assert seen["key"] == DOCUMENT_KEY
    assert seen["content_type"] == "application/json"
    assert seen["cache_control"] == DOCUMENT_CACHE_CONTROL == "public, max-age=60"
    assert json.loads(public_store.read(DOCUMENT_KEY).data) == document


def test_malformed_document_is_withheld_and_the_previous_one_stays(
    document, public_store
):
    """A run that would publish something malformed keeps the previous document."""
    do_publish(document, public_store)
    before = public_store.read(DOCUMENT_KEY)

    broken = copy.deepcopy(document)
    broken["health"]["sources"][0]["state"] = "great"
    with pytest.raises(MalformedDocument) as refusal:
        do_publish(broken, public_store)

    assert refusal.value.problems
    assert public_store.read(DOCUMENT_KEY) == before


def test_document_that_cannot_be_encoded_is_withheld(document, public_store):
    document["meta"]["parameters"]["unexpected"] = {1, 2, 3}  # not JSON at all

    with pytest.raises(MalformedDocument, match="cannot be encoded"):
        do_publish(document, public_store)
    assert public_store.read(DOCUMENT_KEY) is None


def _generated_later(document: dict, by: timedelta) -> dict:
    from datetime import datetime

    later = copy.deepcopy(document)
    later["generated_at"] = (
        datetime.fromisoformat(document["generated_at"]) + by
    ).isoformat()
    return later


def test_an_older_run_does_not_replace_a_newer_document(document, public_store):
    """Overlapping runs: the one that started earlier must not win by finishing last."""
    newer = _generated_later(document, timedelta(minutes=10))
    do_publish(newer, public_store)

    assert do_publish(document, public_store) == "superseded"
    assert json.loads(public_store.read(DOCUMENT_KEY).data) == newer


def test_a_newer_run_replaces_an_older_document(document, public_store):
    do_publish(document, public_store)
    newer = _generated_later(document, timedelta(minutes=10))

    assert do_publish(newer, public_store) == "published"
    assert json.loads(public_store.read(DOCUMENT_KEY).data) == newer


def test_unreadable_previous_document_is_replaced(document, public_store):
    """Garbage at the published address must not block every later publish."""
    public_store.write(DOCUMENT_KEY, b"not json", content_type="application/json")

    assert do_publish(document, public_store) == "published"
    assert json.loads(public_store.read(DOCUMENT_KEY).data) == document


def test_publish_looks_again_when_it_loses_a_race(document, public_store):
    """Another run publishes between our look and our write.

    The conditional write refuses, and the second look sees the other run's
    newer document -- so this run stands down instead of overwriting it.
    """
    newer = _generated_later(document, timedelta(minutes=10))
    real_write = public_store.write
    raced = []

    def racing_write(key, data, **kwargs):
        if not raced:
            raced.append(True)
            real_write(key, serialize(newer), content_type="application/json")
        return real_write(key, data, **kwargs)

    public_store.write = racing_write

    assert do_publish(document, public_store) == "superseded"
    assert json.loads(public_store.read(DOCUMENT_KEY).data) == newer


def test_publish_gives_up_rather_than_retrying_forever(document, public_store):
    def always_loses(*args, **kwargs):
        raise PreconditionFailed("changed again")

    public_store.write = always_loses

    with pytest.raises(StoreError, match="kept changing"):
        do_publish(document, public_store)


def test_storage_failure_is_reported_as_such(document, public_store):
    def down(*args, **kwargs):
        raise StoreError("backend unavailable")

    public_store.write = down

    with pytest.raises(StoreError, match="backend unavailable"):
        do_publish(document, public_store)
