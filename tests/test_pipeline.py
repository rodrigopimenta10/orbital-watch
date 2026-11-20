"""Graceful degradation, end to end.

This is the most important test module in the project. The system's core
promise is that upstream failure cannot stop it from publishing a complete
document that tells the truth about its own state, and the tests below are
what make that a fact rather than an intention.

``test_run_survives_*`` walks every source through failure, one at a time and
then all at once, with and without stored state, and asserts each time that
the run completes, the document passes the same gate production applies, and
the failure is reported rather than hidden.
"""

from __future__ import annotations

import dataclasses
import json
import urllib.error
from datetime import timedelta

import pytest

from orbital_watch import pipeline
from orbital_watch.config import (
    CELESTRAK_MIN_REFETCH_INTERVAL,
    REFRESH_CADENCE,
    SPACE_WEATHER_STALENESS,
    TLE_STALENESS,
    Observer,
)
from orbital_watch.document import SCHEMA_VERSION, validate
from orbital_watch.pipeline import (
    SOURCE_LABELS,
    Deadline,
    build_correlation,
    build_document,
    collect_space_weather,
)
from orbital_watch.sources import swpc
from tests.helpers import (
    ALL_SOURCES_OK,
    CELESTRAK_KEYS,
    FAILURE_MODES,
    SWPC_KEYS,
    FakeClock,
    FakeMonotonic,
    TricklingResponse,
    responder,
    urls,
)

OBSERVER = Observer("Test Site", 39.0840, -77.1528, 82.0)

DEAD_NETWORK = urllib.error.URLError("network is unreachable")


def serve(monkeypatch, mapping, **kwargs) -> list:
    calls: list = []
    monkeypatch.setattr(
        "urllib.request.urlopen", responder(mapping, calls=calls, **kwargs)
    )
    return calls


def run(store, clock) -> dict:
    """One cycle, held to the gate production holds it to."""
    document = build_document(store, observer=OBSERVER, clock=clock)
    problems = validate(document, expected_sources=SOURCE_LABELS)
    assert problems == [], f"the run produced a document it could not publish: {problems}"
    # And it must survive the trip through JSON unchanged.
    assert json.loads(json.dumps(document, allow_nan=False)) == document
    return document


def sources(document) -> dict[str, dict]:
    return {source["name"]: source for source in document["health"]["sources"]}


# --------------------------------------------------------------------------
# Healthy
# --------------------------------------------------------------------------


def test_healthy_run_reports_everything_fresh(state_store, monkeypatch):
    """The happy path still works -- degradation handling has not broken it."""
    serve(monkeypatch, ALL_SOURCES_OK)

    document = run(state_store, FakeClock())

    assert document["schema_version"] == SCHEMA_VERSION
    assert document["health"]["overall"] == "fresh"
    assert document["health"]["counts"] == {"fresh": 7, "stale": 0, "failed": 0}
    assert list(sources(document)) == list(SOURCE_LABELS)
    assert document["meta"]["observer"]["name"] == "Test Site"
    assert document["meta"]["counts"]["tracked_satellites"] > 0
    assert document["sky"]["satellites"], "expected objects near the sky"
    assert document["passes"]["passes"], "expected upcoming passes"
    assert document["passes"]["complete"] is True
    assert document["space_weather"]["weather"]["available"] is True


def test_document_states_the_schedule_it_was_produced_on(state_store, monkeypatch):
    """The page judges the document's age from numbers inside the document."""
    serve(monkeypatch, ALL_SOURCES_OK)
    clock = FakeClock()

    document = run(state_store, clock)

    schedule = document["meta"]["pipeline"]
    assert document["generated_at"] == clock.now.isoformat()
    assert schedule["cadence_seconds"] == REFRESH_CADENCE.total_seconds()
    assert schedule["silence_limit_seconds"] == 3 * REFRESH_CADENCE.total_seconds()
    track = document["sky"]["ephemeris"]
    assert track["start"] == clock.now.isoformat()
    covered = (track["samples"] - 1) * track["step_seconds"]
    assert covered > schedule["cadence_seconds"], (
        "the position track must outlast the wait for the next document"
    )
    thresholds = {
        name: source["thresholds"]["fresh_within_seconds"]
        for name, source in sources(document).items()
    }
    assert thresholds["celestrak_stations"] == TLE_STALENESS.fresh_within.total_seconds()
    assert thresholds["swpc_kp"] == SPACE_WEATHER_STALENESS.fresh_within.total_seconds()


def test_coverage_reports_the_group_size_not_the_sample_size(state_store, monkeypatch):
    """ "60 of 107 recent starlink", never "all 60 recent starlink".

    Only the sampled objects are stored, so on every run after the first the
    group's real size comes from the stored record, not from the data.
    """
    monkeypatch.setattr(
        pipeline,
        "TRACKED_GROUPS",
        tuple(
            dataclasses.replace(group, max_objects=5)
            if group.key == "starlink"
            else group
            for group in pipeline.TRACKED_GROUPS
        ),
    )
    fixture_size = len(json.loads(ALL_SOURCES_OK["GROUP=last-30-days"]))
    serve(monkeypatch, ALL_SOURCES_OK)
    clock = FakeClock()

    first = run(state_store, clock)
    clock.advance(REFRESH_CADENCE)
    second = run(state_store, clock)

    for document in (first, second):
        starlink = next(g for g in document["meta"]["coverage"] if g["key"] == "starlink")
        assert starlink == {
            "key": "starlink",
            "label": "Recent Starlink",
            "available": fixture_size,
            "tracked": 5,
            "sampled": True,
        }
    assert sources(second)["celestrak_starlink"]["outcome"] == "cache_fresh"


# --------------------------------------------------------------------------
# Per-source cadence
# --------------------------------------------------------------------------


def test_each_source_is_asked_at_its_own_cadence(state_store, monkeypatch):
    """Space weather every run; each Celestrak group once per two hours.

    Thirteen runs ten minutes apart cover two hours exactly. Celestrak must
    see one request per group at the start and one more when the floor
    clears -- not thirteen -- while NOAA is asked every time.
    """
    calls = serve(monkeypatch, ALL_SOURCES_OK)
    clock = FakeClock()
    runs = int(CELESTRAK_MIN_REFETCH_INTERVAL / REFRESH_CADENCE) + 1

    for index in range(runs):
        document = run(state_store, clock)
        assert document["health"]["overall"] == "fresh", f"run {index} was not fresh"
        clock.advance(REFRESH_CADENCE)

    asked = urls(calls)
    for key in CELESTRAK_KEYS:
        assert sum(key in url for url in asked) == 2, f"{key} asked too often"
    for key in SWPC_KEYS:
        assert sum(key in url for url in asked) == runs, f"{key} not asked every run"


# --------------------------------------------------------------------------
# GRACEFUL DEGRADATION -- the most important tests in the project
# --------------------------------------------------------------------------


@pytest.mark.parametrize("broken", list(ALL_SOURCES_OK))
@pytest.mark.parametrize("mode", ["timeout", "http_500"])
def test_run_survives_each_source_failing_with_nothing_stored(
    state_store, monkeypatch, broken, mode
):
    """Break one source at a time on a first-ever run.

    This is the project's core promise, checked for every source against
    more than one failure mode. With no stored state the broken source has
    nothing to fall back on, so it must be reported failed -- and every other
    source must be untouched by it.
    """
    serve(monkeypatch, {**ALL_SOURCES_OK, broken: FAILURE_MODES[mode]})

    document = run(state_store, FakeClock())

    health = document["health"]
    assert health["overall"] == "failed"
    assert health["counts"] == {"fresh": 6, "stale": 0, "failed": 1}
    failed = [s for s in health["sources"] if s["state"] == "failed"]
    assert failed[0]["error"], "a failed source must explain why"
    assert failed[0]["last_success"] is None


@pytest.mark.parametrize("broken", list(ALL_SOURCES_OK))
def test_run_serves_stored_data_when_a_source_dies(state_store, monkeypatch, broken):
    """The ordinary degraded state: upstream down, last good copy served.

    The panel must keep showing real numbers and must not call them fresh.
    """
    clock = FakeClock()
    serve(monkeypatch, ALL_SOURCES_OK)
    healthy = run(state_store, clock)

    clock.advance(CELESTRAK_MIN_REFETCH_INTERVAL)  # every source is due again
    serve(monkeypatch, {**ALL_SOURCES_OK, broken: TimeoutError("timed out")})
    document = run(state_store, clock)

    health = document["health"]
    degraded = [s for s in health["sources"] if s["state"] != "fresh"]
    assert len(degraded) == 1
    assert degraded[0]["outcome"] == "cache_fallback"
    assert degraded[0]["last_success"] == healthy["generated_at"]
    assert "unreachable" in degraded[0]["detail"].lower()
    # The data itself is still there.
    assert (
        document["meta"]["counts"]["tracked_satellites"]
        == healthy["meta"]["counts"]["tracked_satellites"]
    )
    assert document["space_weather"]["weather"]["available"] is True


def test_run_survives_total_blackout_with_nothing_stored(state_store, monkeypatch):
    """Every source down on a first-ever run: the worst realistic case.

    We expect a complete, valid document with empty panels and a health block
    that says everything failed rather than pretending otherwise.
    """
    serve(monkeypatch, {}, default=DEAD_NETWORK)

    document = run(state_store, FakeClock())

    health = document["health"]
    assert health["overall"] == "failed"
    assert health["counts"] == {"fresh": 0, "stale": 0, "failed": 7}
    assert all(source["error"] for source in health["sources"])
    assert document["meta"]["counts"]["tracked_satellites"] == 0
    # Panels are empty but structurally valid, so the page has something
    # coherent to render rather than undefined.
    assert document["sky"]["satellites"] == []
    assert document["passes"]["passes"] == []
    assert document["space_weather"]["weather"]["available"] is False
    assert document["space_weather"]["correlation"]["severity"] == "unknown"


def test_run_survives_total_blackout_with_stored_state(state_store, monkeypatch):
    """Every source down, but we ran successfully before.

    Real satellites and real space weather still come out, and not one source
    claims to be fresh: we reached nobody.
    """
    clock = FakeClock()
    serve(monkeypatch, ALL_SOURCES_OK)
    healthy = run(state_store, clock)

    clock.advance(CELESTRAK_MIN_REFETCH_INTERVAL)
    serve(monkeypatch, {}, default=DEAD_NETWORK)
    document = run(state_store, clock)

    health = document["health"]
    assert health["counts"]["fresh"] == 0
    assert health["counts"]["failed"] == 0, "two-hour-old data is stale, not failed"
    assert all(s["outcome"] == "cache_fallback" for s in health["sources"])
    assert all(s["last_success"] == healthy["generated_at"] for s in health["sources"])
    assert document["meta"]["counts"]["tracked_satellites"] > 0
    assert document["sky"]["ephemeris"]["start"] == clock.now.isoformat()
    assert document["space_weather"]["weather"]["kp"] is not None


def test_long_outage_ages_into_failed_on_its_own(state_store, monkeypatch):
    """Stored data does not stay "stale" forever; past the limit it is failed."""
    clock = FakeClock()
    serve(monkeypatch, ALL_SOURCES_OK)
    run(state_store, clock)

    clock.advance(TLE_STALENESS.stale_within + timedelta(minutes=1))
    serve(monkeypatch, {}, default=DEAD_NETWORK)
    document = run(state_store, clock)

    assert document["health"]["counts"] == {"fresh": 0, "stale": 0, "failed": 7}
    assert all(
        "not operationally current" in s["detail"] for s in document["health"]["sources"]
    )


def test_lost_contact_does_not_look_healthy_between_retries(state_store, monkeypatch):
    """A Celestrak failure stays visible on the runs that do not retry it."""
    clock = FakeClock()
    serve(monkeypatch, ALL_SOURCES_OK)
    run(state_store, clock)

    clock.advance(CELESTRAK_MIN_REFETCH_INTERVAL)
    serve(monkeypatch, {**ALL_SOURCES_OK, "GROUP=stations": TimeoutError("timed out")})
    assert sources(run(state_store, clock))["celestrak_stations"]["state"] == "stale"

    clock.advance(REFRESH_CADENCE)
    calls = serve(monkeypatch, ALL_SOURCES_OK)  # upstream is back, but not due
    quiet = sources(run(state_store, clock))["celestrak_stations"]

    assert quiet["state"] == "stale"
    assert quiet["outcome"] == "cache_fallback"
    assert not any("GROUP=stations" in url for url in urls(calls))


@pytest.mark.parametrize(
    "poison",
    [
        {"a dict": "where a list belongs"},
        [1, 2, 3],
        b'"a bare string"',
        [{"unexpected": "keys"}],
        [None, None],
    ],
)
def test_run_survives_upstream_serving_the_wrong_shape(state_store, monkeypatch, poison):
    """Valid JSON of the wrong shape, from every source at once."""
    serve(monkeypatch, dict.fromkeys(ALL_SOURCES_OK, poison))

    document = run(state_store, FakeClock())

    assert document["health"]["counts"] == {"fresh": 0, "stale": 0, "failed": 7}
    assert all("Rejected payload" in s["error"] for s in document["health"]["sources"])
    assert document["meta"]["counts"]["tracked_satellites"] == 0
    assert document["space_weather"]["weather"]["kp"] is None


@pytest.mark.parametrize("broken", SWPC_KEYS)
def test_reshaped_upstream_does_not_replace_good_stored_data(
    state_store, monkeypatch, broken
):
    """An upstream that changes shape must not blank a panel and stay "fresh".

    The response is valid JSON and a non-empty list, so a shallow check would
    accept it, store it over the good copy, and report the source healthy
    while the page showed nothing. It is refused, the stored copy is served,
    and the source reports lost contact.
    """
    clock = FakeClock()
    serve(monkeypatch, ALL_SOURCES_OK)
    healthy = run(state_store, clock)

    clock.advance(REFRESH_CADENCE)
    serve(monkeypatch, {**ALL_SOURCES_OK, broken: [{"renamed_field": 1}]})
    document = run(state_store, clock)

    degraded = [s for s in document["health"]["sources"] if s["state"] != "fresh"]
    assert len(degraded) == 1
    assert degraded[0]["outcome"] == "cache_fallback"
    assert "Rejected payload" in degraded[0]["error"]
    assert document["space_weather"]["weather"] == healthy["space_weather"]["weather"]


def test_upstream_data_gap_is_shown_as_a_gap(state_store, monkeypatch):
    """A null from upstream is a true statement and is passed on as one.

    NOAA reports a gap in the solar wind data as a null value. That is not a
    malformed response: the honest display is a blank tile with the current
    timestamp, not the last number we stored.
    """
    clock = FakeClock()
    serve(monkeypatch, ALL_SOURCES_OK)
    run(state_store, clock)

    clock.advance(REFRESH_CADENCE)
    gap = [{"proton_speed": None, "time_tag": "2026-08-06T15:09:00Z"}]
    serve(monkeypatch, {**ALL_SOURCES_OK, "solar-wind-speed": gap})
    document = run(state_store, clock)

    assert sources(document)["swpc_solar_wind"]["state"] == "fresh"
    weather = document["space_weather"]["weather"]
    assert weather["solar_wind_speed_km_s"] is None
    assert weather["solar_wind_time"] == "2026-08-06T15:09:00+00:00"


def test_stage_that_crashes_is_reported_not_dropped(state_store, monkeypatch):
    """A bug in one stage must not take its sources off the panel.

    If interpreting space weather throws, the document still accounts for all
    four of those sources -- as failed, with the reason -- and the satellite
    panels are unaffected. A panel that silently lost rows would be exactly
    the kind of quiet omission this project argues against.
    """
    serve(monkeypatch, ALL_SOURCES_OK)

    def _bug(*args, **kwargs):
        raise RuntimeError("a bug in the interpretation code")

    monkeypatch.setattr(swpc, "classify_kp", _bug)

    document = run(state_store, FakeClock())

    by_name = sources(document)
    assert list(by_name) == list(SOURCE_LABELS)
    for name in by_name:
        expected = "failed" if name.startswith("swpc_") else "fresh"
        assert by_name[name]["state"] == expected, name
    assert "Internal error" in by_name["swpc_kp"]["error"]
    assert document["meta"]["counts"]["tracked_satellites"] > 0
    assert document["space_weather"]["weather"]["available"] is False


def test_run_out_of_time_publishes_an_incomplete_pass_list_that_says_so(
    state_store, monkeypatch
):
    serve(monkeypatch, ALL_SOURCES_OK)

    document = build_document(
        state_store, observer=OBSERVER, clock=FakeClock(), deadline=Deadline(seconds=0)
    )

    assert validate(document, expected_sources=SOURCE_LABELS) == []
    assert document["passes"]["complete"] is False
    assert document["passes"]["satellites_searched"] == 0
    assert document["passes"]["satellites_total"] > 0
    # Everything that does not depend on the long search is still there.
    assert document["health"]["overall"] == "fresh"
    assert document["sky"]["satellites"]


def test_source_fetched_during_the_run_is_not_negatively_old(state_store, monkeypatch):
    """Regression: "Fetched from upstream in the future ago".

    The document is stamped with the run's start; sources are fetched after
    it. Their age must come out as zero or more, never negative.
    """
    serve(monkeypatch, ALL_SOURCES_OK)
    clock = FakeClock()
    real_fetch = pipeline.fetch_source

    def slow_fetch(*args, **kwargs):
        clock.advance(timedelta(seconds=2))
        return real_fetch(*args, **kwargs)

    monkeypatch.setattr(pipeline, "fetch_source", slow_fetch)
    document = run(state_store, clock)

    for source in document["health"]["sources"]:
        assert source["age_seconds"] >= 0
        assert "future" not in source["detail"]
        assert source["last_success"] > document["generated_at"]


# --------------------------------------------------------------------------
# The correlation banner
# --------------------------------------------------------------------------


def test_correlation_banner_raises_on_a_forced_high_kp(state_store, monkeypatch):
    """Force a storm-level Kp through the real pipeline and assert the banner.

    Verifies the requirement end to end rather than by calling the classifier
    directly: a fabricated upstream payload produces a storm banner in the
    document.
    """
    storm_kp = [
        {"time_tag": "2026-08-06T03:00:00", "Kp": 5.00, "a_running": 48},
        {"time_tag": "2026-08-06T06:00:00", "Kp": 7.67, "a_running": 132},
    ]
    serve(monkeypatch, {**ALL_SOURCES_OK, "planetary-k-index": storm_kp})

    document = run(state_store, FakeClock())
    weather = document["space_weather"]["weather"]
    correlation = document["space_weather"]["correlation"]

    assert weather["kp"] == 7.67
    assert weather["storm"] is True
    assert weather["elevated"] is True
    assert correlation["active"] is True
    assert correlation["severity"] == "storm"
    assert "7.67" in correlation["headline"]
    assert correlation["operational_impacts"], "storm must list operational impacts"
    assert "drag" in correlation["explanation"].lower()


def test_correlation_banner_stays_down_when_quiet(state_store, monkeypatch):
    quiet_kp = [{"time_tag": "2026-08-06T06:00:00", "Kp": 1.33}]
    serve(monkeypatch, {**ALL_SOURCES_OK, "planetary-k-index": quiet_kp})

    weather, _ = collect_space_weather(state_store)
    correlation = build_correlation(weather)

    assert correlation["active"] is False
    assert correlation["severity"] == "quiet"


def test_correlation_is_honest_when_kp_is_unavailable(state_store, monkeypatch):
    """No Kp means we say so, rather than implying conditions are fine."""
    serve(
        monkeypatch,
        {**ALL_SOURCES_OK, "planetary-k-index": TimeoutError("timed out")},
    )

    weather, _ = collect_space_weather(state_store)
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
# Review findings before the first deploy (DECISIONS.md §30)
# --------------------------------------------------------------------------


@pytest.fixture
def monotonic(monkeypatch) -> FakeMonotonic:
    import time

    fake = FakeMonotonic()
    monkeypatch.setattr(time, "monotonic", fake)
    return fake


def throttle(monkeypatch, monotonic, slow_keys) -> None:
    """Upstreams matching ``slow_keys`` send 5 MB at 8 KB/s; the rest are healthy."""
    body = b"[" + b"1," * (5 * 1024 * 1024 // 2) + b"1]"
    healthy = responder(ALL_SOURCES_OK)

    def _urlopen(request, timeout=None):
        if any(key in request.full_url for key in slow_keys):
            return TricklingResponse(body, monotonic)
        return healthy(request, timeout)

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)


def test_throttled_celestrak_cannot_cost_the_run_its_passes_or_its_deadline(
    state_store, monkeypatch, monotonic
):
    """Defect 1, end to end, as it happened on the first real run.

    Celestrak sent every group at a trickle. Before the fix the job read for
    as long as the bytes kept coming: past 150 s the pass list was published
    empty, past 300 s the platform killed the task and nothing was published.
    Now the three groups together cost at most Celestrak's budget, the rest
    of the run is untouched, and the document says what happened.
    """
    from orbital_watch.config import CELESTRAK_NETWORK_BUDGET_SECONDS

    clock = FakeClock()
    serve(monkeypatch, ALL_SOURCES_OK)
    run(state_store, clock)  # a healthy run first, so there is state to serve
    clock.advance(CELESTRAK_MIN_REFETCH_INTERVAL)

    throttle(monkeypatch, monotonic, CELESTRAK_KEYS)
    started = monotonic.now
    document = run(state_store, clock)

    waited = monotonic.now - started
    assert waited <= CELESTRAK_NETWORK_BUDGET_SECONDS + 5, f"waited {waited:.0f}s"
    by_name = sources(document)
    for name in ("celestrak_stations", "celestrak_weather", "celestrak_starlink"):
        assert by_name[name]["outcome"] == "cache_fallback"
        assert "incomplete" in by_name[name]["error"]
    assert by_name["swpc_kp"]["state"] == "fresh"
    assert document["passes"]["complete"] is True
    assert document["passes"]["passes"], "stored element sets still yield passes"


def test_every_upstream_throttled_still_leaves_time_to_compute_and_publish(
    state_store, monkeypatch, monotonic
):
    """The run's overall deadline is real: the network cannot spend the reserves."""
    from orbital_watch.config import (
        COMPUTE_RESERVE_SECONDS,
        HTTP_STALL_TIMEOUT_SECONDS,
        PUBLISH_RESERVE_SECONDS,
        RUN_DEADLINE_SECONDS,
    )

    throttle(monkeypatch, monotonic, list(ALL_SOURCES_OK))
    started = monotonic.now

    document = run(state_store, FakeClock())

    network_limit = (
        RUN_DEADLINE_SECONDS - COMPUTE_RESERVE_SECONDS - PUBLISH_RESERVE_SECONDS
    )
    assert monotonic.now - started <= network_limit + HTTP_STALL_TIMEOUT_SECONDS
    assert document["health"]["counts"] == {"fresh": 0, "stale": 0, "failed": 7}
    assert document["passes"]["complete"] is True


@pytest.mark.parametrize(
    "body",
    [
        b'[{"time_tag": "2026-08-06T12:00:00", "Kp": NaN}]',
        b'[{"time_tag": "2026-08-06T12:00:00", "Kp": Infinity}]',
        b'[{"time_tag": "2026-08-06T12:00:00", "Kp": 1e999}]',
    ],
)
def test_non_finite_upstream_value_degrades_one_source_and_the_run_still_publishes(
    tmp_path, monkeypatch, body
):
    """Defect 2, end to end: this used to exit 3 with nothing published.

    The value was stored, the document built from it failed validation, and
    every run after that failed the same way until upstream changed.
    """
    from orbital_watch import job
    from orbital_watch.store import LocalStore

    state, public = LocalStore(tmp_path / "state"), LocalStore(tmp_path / "public")
    serve(monkeypatch, {**ALL_SOURCES_OK, "planetary-k-index": body})

    assert job.run(state, public) == job.EXIT_OK

    document = json.loads(public.read("latest.json").data)
    assert validate(document, expected_sources=SOURCE_LABELS) == []
    by_name = sources(document)
    assert by_name["swpc_kp"]["state"] == "failed"
    assert (
        "Rejected payload" in by_name["swpc_kp"]["error"]
        or "Malformed" in (by_name["swpc_kp"]["error"])
    )
    assert document["health"]["counts"] == {"fresh": 6, "stale": 0, "failed": 1}
    assert state.read("state/swpc_kp.json") is not None
    assert json.loads(state.read("state/swpc_kp.json").data)["payload"] is None


def test_non_finite_reading_spelt_as_text_is_skipped_and_the_rest_used(
    state_store, monkeypatch
):
    readings = [
        {"time_tag": "2026-08-06T09:00:00", "Kp": "2.67"},
        {"time_tag": "2026-08-06T12:00:00", "Kp": "NaN"},
    ]
    wind = [{"proton_speed": "Infinity", "time_tag": "2026-08-06T14:54:00Z"}]
    serve(
        monkeypatch,
        {**ALL_SOURCES_OK, "planetary-k-index": readings, "solar-wind-speed": wind},
    )

    document = run(state_store, FakeClock())

    weather = document["space_weather"]["weather"]
    assert weather["kp"] == 2.67
    assert weather["solar_wind_speed_km_s"] is None
    assert document["health"]["overall"] == "fresh"


def test_assessment_is_withdrawn_when_the_kp_behind_it_is_no_longer_current(
    state_store, monkeypatch
):
    """ "Storm in progress" must not outlive the data it was read from.

    The banner is written in the present tense. Once the Kp source has gone
    unanswered past its limit the health panel calls it failed -- "not
    operationally current" -- and the banner cannot go on asserting a storm
    on the strength of it.
    """
    storm_kp = [{"time_tag": "2026-08-06T12:00:00", "Kp": 7.67}]
    clock = FakeClock()
    serve(monkeypatch, {**ALL_SOURCES_OK, "planetary-k-index": storm_kp})
    during = run(state_store, clock)["space_weather"]["correlation"]
    assert during["severity"] == "storm"
    assert during["source"] == "swpc_kp"

    clock.advance(SPACE_WEATHER_STALENESS.fresh_within + timedelta(minutes=10))
    serve(
        monkeypatch,
        {**ALL_SOURCES_OK, "planetary-k-index": TimeoutError("timed out")},
    )
    stale = run(state_store, clock)
    assert sources(stale)["swpc_kp"]["state"] == "stale"
    assert stale["space_weather"]["correlation"]["severity"] == "storm", (
        "a stale Kp is still within one Kp interval and still describes now"
    )

    clock.advance(SPACE_WEATHER_STALENESS.stale_within)
    document = run(state_store, clock)
    correlation = document["space_weather"]["correlation"]

    assert sources(document)["swpc_kp"]["state"] == "failed"
    assert correlation["severity"] == "unknown"
    assert correlation["active"] is False
    assert correlation["operational_impacts"] == []
    assert "not current" in correlation["headline"]
    assert "7.67" in correlation["headline"]
    assert "in progress" not in correlation["headline"]
