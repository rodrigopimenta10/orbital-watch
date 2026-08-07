"""The committed snapshot: builds read it, one scheduled job writes it.

These pin the inversion described in §10.3 — the build must not call
Celestrak, and the refresher must be the only thing that does.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

from orbital_watch import health
from orbital_watch.config import TLE_STALENESS, SatelliteGroup
from orbital_watch.refresh import refresh_snapshot, snapshot_age
from orbital_watch.sources import celestrak
from orbital_watch.sources.fetch import Outcome
from tests.helpers import fixture_bytes, responder

GROUP = SatelliteGroup(key="stations", label="Stations", max_objects=50)


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    """An empty snapshot directory wired into both readers and writers."""
    seed = tmp_path / "seed"
    seed.mkdir()
    monkeypatch.setattr("orbital_watch.sources.fetch.SEED_DIR", seed)
    return seed


def write_snapshot(seed, name, records, *, age: timedelta):
    (seed / f"{name}.json").write_text(json.dumps(records))
    (seed / "manifest.json").write_text(
        json.dumps(
            {"sources": {name: {"retrieved_at": (datetime.now(UTC) - age).isoformat()}}}
        )
    )


# --------------------------------------------------------------------------
# The build must not touch Celestrak
# --------------------------------------------------------------------------


def test_build_path_never_calls_celestrak(snapshot, monkeypatch):
    """The default read path is the snapshot, with no network at all.

    The autouse network block would fail this test if a request escaped, but
    assert on the outcome too so the intent is explicit rather than incidental.
    """
    records = json.loads(fixture_bytes("celestrak_stations.json"))
    write_snapshot(snapshot, "celestrak_stations", records, age=timedelta(hours=2))

    result = celestrak.fetch_group(GROUP, snapshot / "unused-cache")

    assert result.outcome is Outcome.SNAPSHOT
    assert result.ok
    assert len(result.data) == len(records)


def test_snapshot_is_fresh_when_young_and_ages_honestly(snapshot):
    """A snapshot is the intended source, so age alone decides its state."""
    records = json.loads(fixture_bytes("celestrak_stations.json"))

    write_snapshot(snapshot, "celestrak_stations", records, age=timedelta(hours=2))
    young = celestrak.fetch_group(GROUP, snapshot / "c")
    assert health.evaluate(young, TLE_STALENESS, label="T").state is health.State.FRESH

    write_snapshot(snapshot, "celestrak_stations", records, age=timedelta(hours=20))
    old = celestrak.fetch_group(GROUP, snapshot / "c")
    assert health.evaluate(old, TLE_STALENESS, label="T").state is health.State.STALE

    write_snapshot(snapshot, "celestrak_stations", records, age=timedelta(days=5))
    ancient = celestrak.fetch_group(GROUP, snapshot / "c")
    assert health.evaluate(ancient, TLE_STALENESS, label="T").state is health.State.FAILED


def test_missing_snapshot_fails_rather_than_reaching_out(snapshot):
    result = celestrak.fetch_group(GROUP, snapshot / "c")

    assert result.outcome is Outcome.FAILED
    assert not result.ok
    assert "snapshot" in result.error.lower()


# --------------------------------------------------------------------------
# The refresher
# --------------------------------------------------------------------------


def test_refresh_writes_every_group(snapshot, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        responder(
            {
                "GROUP=stations": fixture_bytes("celestrak_stations.json"),
                "GROUP=weather": fixture_bytes("celestrak_weather.json"),
                "GROUP=starlink": fixture_bytes("celestrak_starlink.json"),
            }
        ),
    )

    summary = refresh_snapshot(seed_dir=snapshot, cache_dir=tmp_path / "c")

    assert len(summary["refreshed"]) == 3
    assert not summary["failed"]
    manifest = json.loads((snapshot / "manifest.json").read_text())
    for key in ("celestrak_stations", "celestrak_weather", "celestrak_starlink"):
        assert (snapshot / f"{key}.json").exists()
        assert manifest["sources"][key]["records"] > 0


def test_refresh_respects_the_refetch_floor(snapshot, monkeypatch, tmp_path):
    """A young snapshot is left alone -- this is what makes the floor real.

    The floor never applied in production before, because build containers are
    ephemeral and every build started cold. It only means something now that a
    single owner with persistent state enforces it.
    """
    records = json.loads(fixture_bytes("celestrak_stations.json"))
    for key in ("celestrak_stations", "celestrak_weather", "celestrak_starlink"):
        (snapshot / f"{key}.json").write_text(json.dumps(records))
    (snapshot / "manifest.json").write_text(
        json.dumps(
            {
                "sources": {
                    key: {"retrieved_at": datetime.now(UTC).isoformat()}
                    for key in (
                        "celestrak_stations",
                        "celestrak_weather",
                        "celestrak_starlink",
                    )
                }
            }
        )
    )

    def _must_not_be_called(request, timeout=None):
        raise AssertionError("refresher called upstream inside the refetch floor")

    monkeypatch.setattr("urllib.request.urlopen", _must_not_be_called)

    summary = refresh_snapshot(seed_dir=snapshot, cache_dir=tmp_path / "c")

    assert len(summary["skipped"]) == 3
    assert not summary["refreshed"]


def test_refresh_keeps_the_old_snapshot_when_upstream_fails(
    snapshot, monkeypatch, tmp_path
):
    """A failed refresh must not destroy what we already have."""
    records = json.loads(fixture_bytes("celestrak_stations.json"))
    write_snapshot(snapshot, "celestrak_stations", records, age=timedelta(hours=20))

    monkeypatch.setattr(
        "urllib.request.urlopen",
        responder({}, default=urllib.error.URLError("throttled")),
    )

    summary = refresh_snapshot(seed_dir=snapshot, cache_dir=tmp_path / "c")

    assert summary["failed"]
    assert not summary["refreshed"]
    # Previous payload survives untouched.
    assert json.loads((snapshot / "celestrak_stations.json").read_text()) == records


def test_partial_failure_still_writes_the_groups_that_worked(
    snapshot, monkeypatch, tmp_path
):
    """One throttled group must not hold back the two that succeeded."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        responder(
            {
                "GROUP=stations": fixture_bytes("celestrak_stations.json"),
                "GROUP=weather": fixture_bytes("celestrak_weather.json"),
                "GROUP=starlink": TimeoutError("throttled"),
            }
        ),
    )

    summary = refresh_snapshot(seed_dir=snapshot, cache_dir=tmp_path / "c")

    assert sorted(summary["refreshed"]) == ["celestrak_stations", "celestrak_weather"]
    assert summary["failed"] == ["celestrak_starlink"]
    assert (snapshot / "celestrak_stations.json").exists()
    assert not (snapshot / "celestrak_starlink.json").exists()


def test_not_modified_still_advances_the_confirmation_time(
    snapshot, monkeypatch, tmp_path
):
    """Celestrak saying 'you already have the latest' is a successful refresh.

    Regression: the manifest recorded the payload's original download time, so
    a group Celestrak had just confirmed current would age into STALE while
    holding the freshest element sets in existence.
    """
    records = json.loads(fixture_bytes("celestrak_stations.json"))
    write_snapshot(snapshot, "celestrak_stations", records, age=timedelta(hours=20))

    # Warm the cache so the not-modified sentinel has something to serve.
    cache = tmp_path / "c"
    monkeypatch.setattr(
        "urllib.request.urlopen",
        responder({"gp.php": json.loads(fixture_bytes("celestrak_stations.json"))}),
    )
    celestrak.fetch_group(GROUP, cache, allow_network=True)

    monkeypatch.setattr(
        "urllib.request.urlopen",
        responder({"gp.php": fixture_bytes("celestrak_not_updated.txt")}),
    )
    refresh_snapshot(seed_dir=snapshot, cache_dir=cache, force=True)

    manifest = json.loads((snapshot / "manifest.json").read_text())
    entry = manifest["sources"]["celestrak_stations"]
    assert entry["upstream_unchanged"] is True

    age = snapshot_age(snapshot, "celestrak_stations")
    assert age is not None and age < timedelta(minutes=1), (
        "a confirmed-current snapshot must not keep the stale payload timestamp"
    )


def test_snapshot_coverage_reports_upstream_population_not_trimmed_count(
    snapshot, monkeypatch, tmp_path
):
    """The sampling disclosure must survive the snapshot being the source.

    Regression: the snapshot stores trimmed records, so taking `available`
    from len(snapshot) made the page claim "all 60 starlink" — the honesty
    feature inverted into a false completeness claim. The refresher records
    the pre-trim population in the manifest, and the build must read it back.
    """
    from orbital_watch.build import collect_satellites
    from orbital_watch.propagate import timescale

    # Force trimming: the committed starlink fixture is itself already trimmed
    # (40 records), so cap the group below that or nothing gets sampled and
    # this test asserts nothing.
    tight = (
        SatelliteGroup(key="stations", label="Space Stations", max_objects=40),
        SatelliteGroup(key="weather", label="Weather Satellites", max_objects=90),
        SatelliteGroup(key="starlink", label="Starlink", max_objects=10),
    )
    monkeypatch.setattr("orbital_watch.refresh.TRACKED_GROUPS", tight)
    monkeypatch.setattr("orbital_watch.build.TRACKED_GROUPS", tight)

    # Refresh from a fixture where upstream offers more than the cap keeps.
    monkeypatch.setattr(
        "urllib.request.urlopen",
        responder(
            {
                "GROUP=stations": fixture_bytes("celestrak_stations.json"),
                "GROUP=weather": fixture_bytes("celestrak_weather.json"),
                "GROUP=starlink": fixture_bytes("celestrak_starlink.json"),
            }
        ),
    )
    refresh_snapshot(seed_dir=snapshot, cache_dir=tmp_path / "c")

    manifest = json.loads((snapshot / "manifest.json").read_text())
    starlink_upstream = manifest["sources"]["celestrak_starlink"]["available"]
    starlink_kept = manifest["sources"]["celestrak_starlink"]["records"]
    assert starlink_upstream > starlink_kept, (
        "fixture must exercise trimming for this test to mean anything"
    )

    _, _, coverage = collect_satellites(tmp_path / "cache", timescale())

    starlink = next(g for g in coverage if g["key"] == "starlink")
    assert starlink["available"] == starlink_upstream
    assert starlink["sampled"] is True


# --------------------------------------------------------------------------
# Audit regressions: shapes of damage the refresher must survive
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "damage",
    [
        "[]",  # a list where the dict belongs
        '"a string"',
        '{"sources": []}',  # valid dict, wrong-shaped sources
        '{"sources": "nope"}',
        "not json at all {",
    ],
)
def test_refresher_survives_any_manifest_damage(snapshot, monkeypatch, tmp_path, damage):
    """A malformed manifest must self-heal, never wedge the refresher.

    Regression: a valid-JSON-wrong-shape manifest raised TypeError past the
    OSError-only handler in main(), and because nothing repaired the file,
    every subsequent scheduled run crashed identically while the snapshot
    aged toward FAILED. The one component whose job is keeping data fresh
    must not be permanently disabled by a damaged state file.
    """
    (snapshot / "manifest.json").write_text(damage)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        responder(
            {
                "GROUP=stations": fixture_bytes("celestrak_stations.json"),
                "GROUP=weather": fixture_bytes("celestrak_weather.json"),
                "GROUP=starlink": fixture_bytes("celestrak_starlink.json"),
            }
        ),
    )

    summary = refresh_snapshot(seed_dir=snapshot, cache_dir=tmp_path / "c")

    assert len(summary["refreshed"]) == 3
    manifest = json.loads((snapshot / "manifest.json").read_text())
    assert isinstance(manifest["sources"], dict)


def test_shrunken_payload_does_not_replace_a_substantial_snapshot(
    snapshot, monkeypatch, tmp_path
):
    """A validating payload that collapsed in size keeps the old snapshot.

    Constellations do not lose most of their objects between refreshes; a
    2-record response where the snapshot holds 22 is upstream trouble.
    """
    records = json.loads(fixture_bytes("celestrak_stations.json"))
    write_snapshot(snapshot, "celestrak_stations", records, age=timedelta(hours=20))
    manifest = json.loads((snapshot / "manifest.json").read_text())
    manifest["sources"]["celestrak_stations"]["records"] = len(records)
    (snapshot / "manifest.json").write_text(json.dumps(manifest))

    tiny = records[:2]
    monkeypatch.setattr(
        "urllib.request.urlopen", responder({"GROUP=stations": tiny}, default=tiny)
    )

    summary = refresh_snapshot(seed_dir=snapshot, cache_dir=tmp_path / "c")

    assert "celestrak_stations" in summary["failed"]
    kept = json.loads((snapshot / "celestrak_stations.json").read_text())
    assert len(kept) == len(records), "shrunken payload must not replace the snapshot"


def test_future_capture_time_does_not_block_refresh_forever(
    snapshot, monkeypatch, tmp_path
):
    """A manifest timestamp in the future must trigger a refresh, not a skip.

    Negative age is < min_age forever, so without the guard a clock-skewed or
    hand-edited timestamp silently blocked refresh until wall-clock caught up.
    """
    records = json.loads(fixture_bytes("celestrak_stations.json"))
    write_snapshot(snapshot, "celestrak_stations", records, age=-timedelta(days=365))

    monkeypatch.setattr(
        "urllib.request.urlopen",
        responder(
            {"GROUP=stations": fixture_bytes("celestrak_stations.json")},
            default=urllib.error.URLError("other groups absent"),
        ),
    )

    summary = refresh_snapshot(seed_dir=snapshot, cache_dir=tmp_path / "c")

    assert "celestrak_stations" in summary["refreshed"]


def test_cold_runner_not_modified_counts_as_confirmation(snapshot, monkeypatch, tmp_path):
    """Celestrak's sentinel on a cold runner must confirm, not fail.

    CI runners have no fetch cache, and the sentinel is tracked per IP --
    shared egress can fire it on this runner's first contact. Without the
    pre-warm, "you already hold the newest data" resolved to FAILED and the
    cycle was dropped while seed/ held exactly the confirmed data.
    """
    records = json.loads(fixture_bytes("celestrak_stations.json"))
    write_snapshot(snapshot, "celestrak_stations", records, age=timedelta(hours=20))

    monkeypatch.setattr(
        "urllib.request.urlopen",
        responder(
            {"GROUP=stations": fixture_bytes("celestrak_not_updated.txt")},
            default=urllib.error.URLError("other groups absent"),
        ),
    )

    # cache_dir is a fresh tmp dir: the cold-runner case.
    summary = refresh_snapshot(seed_dir=snapshot, cache_dir=tmp_path / "coldcache")

    assert "celestrak_stations" in summary["refreshed"]
    manifest = json.loads((snapshot / "manifest.json").read_text())
    entry = manifest["sources"]["celestrak_stations"]
    assert entry["upstream_unchanged"] is True
    age = snapshot_age(snapshot, "celestrak_stations")
    assert age is not None and age < timedelta(minutes=1)
