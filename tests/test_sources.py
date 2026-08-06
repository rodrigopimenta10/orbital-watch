"""Source fetching and graceful degradation.

This is the most important test module in the project. The site's core promise
is that upstream failure cannot take it down, and the tests below are what
make that a fact rather than an intention.

``test_build_survives_*`` walks every source through failure, one at a time
and then all at once, and asserts the build still completes and reports the
failure honestly.
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orbital_watch.build import build_correlation, collect_space_weather, run_build
from orbital_watch.config import (
    DATA_SUBDIR,
    Observer,
    SatelliteGroup,
    StalenessPolicy,
)
from orbital_watch.sources import celestrak, swpc
from orbital_watch.sources.fetch import Outcome, fetch_json, format_duration

OBSERVER = Observer("Test Site", 39.0840, -77.1528, 82.0)


# --------------------------------------------------------------------------
# Fake HTTP plumbing
# --------------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    """Minimal stand-in for the object urlopen returns."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def responder(mapping: dict[str, object], *, default=None):
    """Build a fake urlopen that serves payloads by URL substring.

    A mapping value may be bytes/str (served verbatim), a JSON-able object, or
    an Exception instance (raised, to simulate that source failing).
    """

    def _urlopen(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        for needle, payload in mapping.items():
            if needle in url:
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, bytes):
                    return FakeResponse(payload)
                if isinstance(payload, str):
                    return FakeResponse(payload.encode())
                return FakeResponse(json.dumps(payload).encode())
        if default is None:
            raise urllib.error.URLError("no route to host (unmapped in test)")
        if isinstance(default, Exception):
            raise default
        return FakeResponse(json.dumps(default).encode())

    return _urlopen


def fixture_bytes(name: str) -> bytes:
    return (Path(__file__).parent / "fixtures" / name).read_bytes()


ALL_SOURCES_OK = {
    "GROUP=stations": fixture_bytes("celestrak_stations.json"),
    "GROUP=weather": fixture_bytes("celestrak_weather.json"),
    "GROUP=starlink": fixture_bytes("celestrak_starlink.json"),
    "planetary-k-index": fixture_bytes("swpc_kindex.json"),
    "solar-wind-speed": fixture_bytes("swpc_solar_wind_speed.json"),
    "solar-wind-mag-field": fixture_bytes("swpc_solar_wind_mag.json"),
    "solar-cycle": fixture_bytes("swpc_solar_cycle.json"),
}

#: Every upstream key, for the "fail one at a time" sweep.
SOURCE_KEYS = list(ALL_SOURCES_OK)

FAILURE_MODES = {
    "timeout": TimeoutError("timed out"),
    "dns": urllib.error.URLError("Name or service not known"),
    "http_500": urllib.error.HTTPError(
        "https://example.invalid", 500, "Internal Server Error", {}, None
    ),
    "http_403": urllib.error.HTTPError(
        "https://example.invalid", 403, "Forbidden", {}, None
    ),
}


# --------------------------------------------------------------------------
# The fetch helper itself
# --------------------------------------------------------------------------


def test_successful_fetch_populates_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", responder({"data.json": {"hello": "world"}})
    )

    result = fetch_json("demo", "https://example.invalid/data.json", tmp_path)

    assert result.outcome is Outcome.LIVE
    assert result.data == {"hello": "world"}
    assert result.last_success is not None
    assert (tmp_path / "demo.json").exists()
    assert (tmp_path / "demo.json.meta").exists()


@pytest.mark.parametrize("mode", sorted(FAILURE_MODES))
def test_fetch_falls_back_to_cache_on_every_failure_mode(tmp_path, monkeypatch, mode):
    """Whatever goes wrong upstream, a warm cache keeps us serving."""
    monkeypatch.setattr(
        "urllib.request.urlopen", responder({"data.json": {"cached": True}})
    )
    fetch_json("demo", "https://example.invalid/data.json", tmp_path)

    monkeypatch.setattr(
        "urllib.request.urlopen", responder({"data.json": FAILURE_MODES[mode]})
    )
    result = fetch_json("demo", "https://example.invalid/data.json", tmp_path)

    assert result.outcome is Outcome.CACHE_FALLBACK
    assert result.data == {"cached": True}
    assert result.ok
    assert result.error


@pytest.mark.parametrize("mode", sorted(FAILURE_MODES))
def test_fetch_reports_failure_when_there_is_no_cache(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(
        "urllib.request.urlopen", responder({"data.json": FAILURE_MODES[mode]})
    )

    result = fetch_json("demo", "https://example.invalid/data.json", tmp_path)

    assert result.outcome is Outcome.FAILED
    assert result.data is None
    assert not result.ok
    assert result.error


def test_fetch_never_raises_even_on_unexpected_errors(tmp_path, monkeypatch):
    """The helper's contract is that callers never need a try/except."""

    def _explode(request, timeout=None):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr("urllib.request.urlopen", _explode)

    result = fetch_json("demo", "https://example.invalid/data.json", tmp_path)

    assert result.outcome is Outcome.FAILED
    assert "something nobody anticipated" in result.error


def test_malformed_json_does_not_overwrite_a_good_cache(tmp_path, monkeypatch):
    """A garbage response must not poison the cache we would fall back to."""
    monkeypatch.setattr(
        "urllib.request.urlopen", responder({"data.json": {"good": True}})
    )
    fetch_json("demo", "https://example.invalid/data.json", tmp_path)

    monkeypatch.setattr(
        "urllib.request.urlopen", responder({"data.json": b"<html>not json</html>"})
    )
    result = fetch_json("demo", "https://example.invalid/data.json", tmp_path)

    assert result.outcome is Outcome.CACHE_FALLBACK
    assert result.data == {"good": True}
    assert json.loads((tmp_path / "demo.json").read_text()) == {"good": True}


def test_celestrak_not_updated_sentinel_is_treated_as_not_modified(tmp_path, monkeypatch):
    """Celestrak's 200-with-plaintext 'not updated' reply is not an error.

    Re-requesting a GP group inside its 2h refresh window returns HTTP 200
    with a plain sentence, not JSON. Handled naively this either crashes the
    build or overwrites the cache with prose. It means 'nothing new', so we
    serve cache and stay healthy.
    """
    monkeypatch.setattr(
        "urllib.request.urlopen", responder({"gp.php": [{"OBJECT_NAME": "SAT"}]})
    )
    fetch_json("gp", "https://celestrak.org/gp.php?GROUP=x", tmp_path)

    sentinel = fixture_bytes("celestrak_not_updated.txt")
    monkeypatch.setattr("urllib.request.urlopen", responder({"gp.php": sentinel}))
    result = fetch_json("gp", "https://celestrak.org/gp.php?GROUP=x", tmp_path)

    assert result.outcome is Outcome.NOT_MODIFIED
    assert result.data == [{"OBJECT_NAME": "SAT"}]
    # Crucially, the cache still holds JSON and not the sentence.
    assert json.loads((tmp_path / "gp.json").read_text()) == [{"OBJECT_NAME": "SAT"}]


def test_not_updated_sentinel_is_detected_even_with_an_http_403(tmp_path, monkeypatch):
    """Celestrak sends the not-updated sentinel with a 403, not a 200.

    Observed against the live API: re-requesting a group inside its refresh
    window returns HTTP 403 whose *body* is the "GP data has not updated"
    sentence. Checking the body only on the success path misses it entirely,
    and a benign "nothing changed" gets reported as a hard failure while a
    good cache sits unused. Regression test for that.
    """
    monkeypatch.setattr(
        "urllib.request.urlopen", responder({"gp.php": [{"OBJECT_NAME": "SAT"}]})
    )
    fetch_json("gp", "https://celestrak.org/gp.php?GROUP=starlink", tmp_path)

    sentinel = fixture_bytes("celestrak_not_updated.txt")

    def _forbidden_with_sentinel(request, timeout=None):
        raise urllib.error.HTTPError(
            "https://celestrak.org/gp.php",
            403,
            "Forbidden",
            {},
            io.BytesIO(sentinel),
        )

    monkeypatch.setattr("urllib.request.urlopen", _forbidden_with_sentinel)
    result = fetch_json("gp", "https://celestrak.org/gp.php?GROUP=starlink", tmp_path)

    assert result.outcome is Outcome.NOT_MODIFIED
    assert result.data == [{"OBJECT_NAME": "SAT"}]
    assert result.ok


def test_a_genuine_403_is_still_a_failure(tmp_path, monkeypatch):
    """Only the sentinel body is forgiven -- a real 403 must still degrade."""

    def _plain_forbidden(request, timeout=None):
        raise urllib.error.HTTPError(
            "https://celestrak.org/gp.php",
            403,
            "Forbidden",
            {},
            io.BytesIO(b"go away"),
        )

    monkeypatch.setattr("urllib.request.urlopen", _plain_forbidden)
    result = fetch_json("gp", "https://celestrak.org/gp.php", tmp_path)

    assert result.outcome is Outcome.FAILED
    assert "403" in result.error


def test_rate_limit_guard_skips_the_network_entirely(tmp_path, monkeypatch):
    """A young cache means we do not call Celestrak at all."""
    monkeypatch.setattr("urllib.request.urlopen", responder({"gp.php": [{"a": 1}]}))
    fetch_json("gp", "https://celestrak.org/gp.php", tmp_path)

    def _must_not_be_called(request, timeout=None):
        raise AssertionError("network was called despite a young cache")

    monkeypatch.setattr("urllib.request.urlopen", _must_not_be_called)
    result = fetch_json(
        "gp",
        "https://celestrak.org/gp.php",
        tmp_path,
        min_refetch_interval=timedelta(hours=6),
    )

    assert result.outcome is Outcome.CACHE_FRESH
    assert result.data == [{"a": 1}]


def test_rate_limit_guard_expires(tmp_path, monkeypatch):
    """An old cache does not block a refetch."""
    monkeypatch.setattr("urllib.request.urlopen", responder({"gp.php": [{"a": 1}]}))
    fetch_json("gp", "https://celestrak.org/gp.php", tmp_path)

    stale_time = (datetime.now(UTC) - timedelta(hours=12)).isoformat()
    (tmp_path / "gp.json.meta").write_text(json.dumps({"retrieved_at": stale_time}))

    monkeypatch.setattr("urllib.request.urlopen", responder({"gp.php": [{"a": 2}]}))
    result = fetch_json(
        "gp",
        "https://celestrak.org/gp.php",
        tmp_path,
        min_refetch_interval=timedelta(hours=6),
    )

    assert result.outcome is Outcome.LIVE
    assert result.data == [{"a": 2}]


# --------------------------------------------------------------------------
# Payload validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"not": "a list"},
        [],
        [{"OBJECT_NAME": "MISSING EVERYTHING ELSE"}],
        ["not a dict"],
    ],
)
def test_unusable_gp_payloads_are_rejected(tmp_path, monkeypatch, payload):
    """Structurally wrong GP data is refused rather than propagated."""
    monkeypatch.setattr("urllib.request.urlopen", responder({"gp.php": payload}))
    group = SatelliteGroup(key="stations", label="Stations", max_objects=10)

    result = celestrak.fetch_group(group, tmp_path)

    assert result.outcome is Outcome.FAILED
    assert result.data is None


def test_partially_valid_gp_payload_keeps_the_good_records(tmp_path, monkeypatch):
    good = json.loads(fixture_bytes("celestrak_stations.json"))
    mixed = [{"OBJECT_NAME": "JUNK"}, *good]
    monkeypatch.setattr("urllib.request.urlopen", responder({"gp.php": mixed}))
    group = SatelliteGroup(key="stations", label="Stations", max_objects=100)

    result = celestrak.fetch_group(group, tmp_path)

    assert result.ok
    assert len(result.data) == len(good)


def test_select_objects_caps_group_size():
    records = json.loads(fixture_bytes("celestrak_starlink.json"))
    group = SatelliteGroup(key="starlink", label="Starlink", max_objects=5)

    selected = celestrak.select_objects(records, group)

    assert len(selected) == 5
    # We keep the most recent epochs, which carry the least propagation error.
    epochs = [r["EPOCH"] for r in selected]
    assert epochs == sorted(epochs, reverse=True)


# --------------------------------------------------------------------------
# Space weather parsing and interpretation
# --------------------------------------------------------------------------


def test_kp_series_parses_the_live_dict_shape(kp_records):
    readings = swpc.parse_kp_series(kp_records)

    assert readings
    assert all(0 <= r.kp <= 9 for r in readings)
    assert readings == sorted(readings, key=lambda r: r.time_tag)


def test_kp_series_parses_the_legacy_list_of_lists_shape():
    """SWPC has shipped both shapes; we accept either rather than assume."""
    legacy = [
        ["time_tag", "Kp", "a_running", "station_count"],
        ["2026-08-06T00:00:00", "3.33", "18", "8"],
        ["2026-08-06T03:00:00", "4.67", "22", "8"],
    ]
    readings = swpc.parse_kp_series(legacy)

    assert [r.kp for r in readings] == [3.33, 4.67]


def test_kp_series_survives_an_unrecognised_shape():
    assert swpc.parse_kp_series([{"totally": "different"}]) == []
    assert swpc.parse_kp_series([["unknown", "header"], ["a", "b"]]) == []
    assert swpc.parse_kp_series([]) == []


@pytest.mark.parametrize(
    ("kp", "label"),
    [
        (0.0, "quiet"),
        (2.67, "quiet"),
        (3.0, "unsettled"),
        (3.99, "unsettled"),
        (4.0, "active"),
        (5.0, "minor storm"),
        (6.0, "moderate"),
        (7.0, "strong"),
        (8.0, "severe"),
        (9.0, "extreme"),
    ],
)
def test_kp_classification_boundaries(kp, label):
    assert swpc.classify_kp(kp)[0] == label


def test_kp_trend_direction():
    def series(values):
        return [
            swpc.KpReading(datetime(2026, 8, 6, h, tzinfo=UTC), v)
            for h, v in enumerate(values)
        ]

    assert swpc.kp_trend(series([1.0, 2.0, 3.0, 4.0])) == "rising"
    assert swpc.kp_trend(series([5.0, 4.0, 3.0, 2.0])) == "falling"
    assert swpc.kp_trend(series([3.0, 3.3, 3.0, 3.3])) == "steady"
    assert swpc.kp_trend([]) == "unknown"


# --------------------------------------------------------------------------
# The correlation banner
# --------------------------------------------------------------------------


def test_correlation_banner_raises_on_a_forced_high_kp(tmp_path, monkeypatch):
    """Force a storm-level Kp through the real pipeline and assert the banner.

    Verifies the requirement end to end rather than by calling the classifier
    directly: a fabricated upstream payload produces a storm banner in the
    output JSON.
    """
    storm_kp = [
        {"time_tag": "2026-08-06T03:00:00", "Kp": 5.00, "a_running": 48},
        {"time_tag": "2026-08-06T06:00:00", "Kp": 7.67, "a_running": 132},
    ]
    monkeypatch.setattr(
        "urllib.request.urlopen",
        responder({**ALL_SOURCES_OK, "planetary-k-index": storm_kp}),
    )

    weather, _ = collect_space_weather(tmp_path)
    correlation = build_correlation(weather)

    assert weather["kp"] == 7.67
    assert weather["storm"] is True
    assert weather["elevated"] is True
    assert correlation["active"] is True
    assert correlation["severity"] == "storm"
    assert "7.67" in correlation["headline"]
    assert correlation["operational_impacts"], "storm must list operational impacts"
    assert "drag" in correlation["explanation"].lower()


def test_correlation_banner_stays_down_when_quiet(tmp_path, monkeypatch):
    quiet_kp = [{"time_tag": "2026-08-06T06:00:00", "Kp": 1.33}]
    monkeypatch.setattr(
        "urllib.request.urlopen",
        responder({**ALL_SOURCES_OK, "planetary-k-index": quiet_kp}),
    )

    weather, _ = collect_space_weather(tmp_path)
    correlation = build_correlation(weather)

    assert correlation["active"] is False
    assert correlation["severity"] == "quiet"


def test_correlation_is_honest_when_kp_is_unavailable(tmp_path, monkeypatch):
    """No Kp means we say so, rather than implying conditions are fine."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        responder({**ALL_SOURCES_OK, "planetary-k-index": TimeoutError("timed out")}),
    )

    weather, _ = collect_space_weather(tmp_path)
    correlation = build_correlation(weather)

    assert weather["available"] is False
    assert correlation["severity"] == "unknown"
    assert correlation["active"] is False
    assert "unavailable" in correlation["headline"].lower()


@pytest.mark.parametrize("kp", [4.0, 4.33, 4.99])
def test_elevated_band_raises_the_banner_below_storm_level(kp):
    weather = {
        "available": True,
        "kp": kp,
        "storm": swpc.is_storm(kp),
        "elevated": swpc.is_elevated(kp),
    }
    correlation = build_correlation(weather)

    assert correlation["active"] is True
    assert correlation["severity"] == "elevated"


# --------------------------------------------------------------------------
# GRACEFUL DEGRADATION -- the most important tests in the project
# --------------------------------------------------------------------------


def assert_site_is_complete(build_dir: Path) -> dict:
    """Every expected artefact exists and parses. Returns the health payload."""
    data_dir = build_dir / DATA_SUBDIR
    expected = [
        "meta.json",
        "sky.json",
        "passes.json",
        "space_weather.json",
        "health.json",
    ]

    for name in expected:
        path = data_dir / name
        assert path.exists(), f"{name} was not written"
        json.loads(path.read_text())  # must parse

    return json.loads((data_dir / "health.json").read_text())


@pytest.mark.parametrize("broken", SOURCE_KEYS)
@pytest.mark.parametrize("mode", ["timeout", "http_500"])
def test_build_survives_each_source_failing_individually(
    tmp_path, monkeypatch, broken, mode
):
    """Break one source at a time; the build must still produce a full site.

    This is the project's core promise, checked for every source against
    multiple failure modes.
    """
    mapping = {**ALL_SOURCES_OK, broken: FAILURE_MODES[mode]}
    monkeypatch.setattr("urllib.request.urlopen", responder(mapping))

    run_build(
        cache_dir=tmp_path / "cache",
        build_dir=tmp_path / "dist",
        web_dir=tmp_path / "nonexistent-web",
        observer=OBSERVER,
    )

    health = assert_site_is_complete(tmp_path / "dist")

    # The failure is reported, not hidden.
    assert health["overall"] in ("stale", "failed")
    assert health["counts"]["failed"] >= 1
    failed = [s for s in health["sources"] if s["state"] == "failed"]
    assert failed, "a source failed but nothing was reported as failed"
    assert all(s["error"] for s in failed), "a failed source must explain why"


def test_build_survives_total_upstream_blackout(tmp_path, monkeypatch):
    """Every single source down, cold cache. The site must still render.

    The worst realistic case: no network at all on a fresh clone. We expect a
    valid site, empty data panels, and a health block that says everything is
    failed rather than pretending otherwise.
    """
    monkeypatch.setattr(
        "urllib.request.urlopen",
        responder({}, default=urllib.error.URLError("network is unreachable")),
    )

    meta = run_build(
        cache_dir=tmp_path / "cache",
        build_dir=tmp_path / "dist",
        web_dir=tmp_path / "nonexistent-web",
        observer=OBSERVER,
    )

    health = assert_site_is_complete(tmp_path / "dist")

    assert health["overall"] == "failed"
    assert health["counts"]["fresh"] == 0
    assert health["counts"]["failed"] == len(health["sources"])
    assert meta["counts"]["tracked_satellites"] == 0
    assert meta["counts"]["above_horizon"] == 0

    # Panels are empty but structurally valid, so the frontend has something
    # coherent to render rather than undefined.
    sky = json.loads((tmp_path / "dist" / DATA_SUBDIR / "sky.json").read_text())
    passes = json.loads((tmp_path / "dist" / DATA_SUBDIR / "passes.json").read_text())
    weather = json.loads(
        (tmp_path / "dist" / DATA_SUBDIR / "space_weather.json").read_text()
    )
    assert sky["satellites"] == []
    assert passes["passes"] == []
    assert weather["weather"]["available"] is False
    assert weather["correlation"]["severity"] == "unknown"


def test_build_serves_cached_data_when_upstream_dies(tmp_path, seeded_cache):
    """With a warm cache and a dead network, we still publish real numbers.

    This is the normal degraded state in production: the scheduled job runs,
    upstream is down, and the site keeps showing yesterday's element sets
    while clearly labelling them as stale.
    """
    import urllib.request

    def _dead(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    original = urllib.request.urlopen
    urllib.request.urlopen = _dead
    try:
        meta = run_build(
            cache_dir=seeded_cache,
            build_dir=tmp_path / "dist",
            web_dir=tmp_path / "nonexistent-web",
            observer=OBSERVER,
        )
    finally:
        urllib.request.urlopen = original

    health = assert_site_is_complete(tmp_path / "dist")

    # Real satellites came out of the cache.
    assert meta["counts"]["tracked_satellites"] > 0
    # But nothing claims to be fresh -- we could not reach anyone.
    assert health["counts"]["fresh"] == 0
    assert health["overall"] == "stale"
    assert all(s["outcome"] == "cache_fallback" for s in health["sources"])
    assert all("unreachable" in s["detail"].lower() for s in health["sources"])


def test_build_completes_when_everything_is_healthy(tmp_path, monkeypatch):
    """The happy path still works -- degradation handling has not broken it."""
    monkeypatch.setattr("urllib.request.urlopen", responder(ALL_SOURCES_OK))

    meta = run_build(
        cache_dir=tmp_path / "cache",
        build_dir=tmp_path / "dist",
        web_dir=tmp_path / "nonexistent-web",
        observer=OBSERVER,
    )

    health = assert_site_is_complete(tmp_path / "dist")

    assert health["overall"] == "fresh"
    assert health["counts"]["failed"] == 0
    assert meta["counts"]["tracked_satellites"] > 0
    assert meta["schema_version"] >= 1
    assert meta["observer"]["name"] == "Test Site"


def test_build_copies_the_frontend_into_the_output(tmp_path, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", responder(ALL_SOURCES_OK))
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<h1>Orbital Watch</h1>")
    (web / "assets" / "app.js").write_text("// app")

    run_build(
        cache_dir=tmp_path / "cache",
        build_dir=tmp_path / "dist",
        web_dir=web,
        observer=OBSERVER,
    )

    assert (tmp_path / "dist" / "index.html").read_text() == "<h1>Orbital Watch</h1>"
    assert (tmp_path / "dist" / "assets" / "app.js").exists()
    assert (tmp_path / "dist" / DATA_SUBDIR / "meta.json").exists()


# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=0), "0s"),
        (timedelta(seconds=45), "45s"),
        (timedelta(minutes=5), "5m"),
        (timedelta(hours=1), "1h"),
        (timedelta(hours=3, minutes=12), "3h 12m"),
        (timedelta(days=2), "2d"),
        (timedelta(days=1, hours=6), "1d 6h"),
    ],
)
def test_duration_formatting(delta, expected):
    assert format_duration(delta) == expected


def test_staleness_policy_is_explicit_not_magic():
    """Thresholds must be declared constants the panel can publish."""
    from orbital_watch.config import SPACE_WEATHER_STALENESS, TLE_STALENESS

    for policy in (TLE_STALENESS, SPACE_WEATHER_STALENESS):
        assert isinstance(policy, StalenessPolicy)
        assert policy.fresh_within < policy.stale_within
