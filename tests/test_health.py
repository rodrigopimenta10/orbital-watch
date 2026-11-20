"""Staleness classification, including exact-threshold boundaries.

The health panel is only worth having if its states mean precisely what the
published thresholds say they mean. These tests pin the boundaries, including
the exactly-on-the-line cases where off-by-one errors live.

Convention under test: the comparison is ``age > threshold``, so an age
exactly equal to a threshold is still inside it. A source whose last success
was exactly 8h ago, with an 8h freshness window, is FRESH.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orbital_watch.config import StalenessPolicy
from orbital_watch.health import (
    State,
    evaluate,
    overall_state,
    summarize,
)
from orbital_watch.sources.fetch import FetchResult, Outcome

NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)

POLICY = StalenessPolicy(
    fresh_within=timedelta(hours=8),
    stale_within=timedelta(hours=48),
)


#: Distinguishes "caller did not specify data" from "caller explicitly wants
#: no data", which is the case that must classify as FAILED.
_UNSET = object()


def make_result(
    *,
    data=_UNSET,
    outcome=Outcome.LIVE,
    age: timedelta | None = timedelta(0),
    error: str | None = None,
) -> FetchResult:
    """Build a FetchResult positioned a given age before NOW."""
    return FetchResult(
        name="test_source",
        url="https://example.invalid/data.json",
        data=[{"value": 1}] if data is _UNSET else data,
        outcome=outcome,
        last_success=(NOW - age) if age is not None else None,
        elapsed_seconds=0.1,
        error=error,
    )


# --------------------------------------------------------------------------
# Boundaries
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        # Well inside the freshness window.
        (timedelta(0), State.FRESH),
        (timedelta(hours=1), State.FRESH),
        (timedelta(hours=7, minutes=59), State.FRESH),
        # Exactly on the freshness threshold -- still fresh.
        (timedelta(hours=8), State.FRESH),
        # One second past it -- stale.
        (timedelta(hours=8, seconds=1), State.STALE),
        (timedelta(hours=24), State.STALE),
        (timedelta(hours=47, minutes=59), State.STALE),
        # Exactly on the stale threshold -- still stale.
        (timedelta(hours=48), State.STALE),
        # One second past it -- failed.
        (timedelta(hours=48, seconds=1), State.FAILED),
        (timedelta(days=7), State.FAILED),
    ],
)
def test_age_boundaries(age, expected):
    result = make_result(age=age)
    assert evaluate(result, POLICY, label="Test", now=NOW).state is expected


def test_thresholds_are_reported_verbatim():
    """The panel publishes the thresholds so a viewer can check our arithmetic."""
    health = evaluate(make_result(), POLICY, label="Test", now=NOW)
    payload = health.to_dict()

    assert payload["thresholds"]["fresh_within_seconds"] == 8 * 3600
    assert payload["thresholds"]["stale_within_seconds"] == 48 * 3600


# --------------------------------------------------------------------------
# Outcome-driven states
# --------------------------------------------------------------------------


def test_no_data_is_failed_regardless_of_age():
    result = make_result(data=None, outcome=Outcome.FAILED, age=None, error="HTTP 503")
    health = evaluate(result, POLICY, label="Test", now=NOW)

    assert health.state is State.FAILED
    assert health.age_seconds is None
    assert health.error == "HTTP 503"
    assert "HTTP 503" in health.detail


def test_cache_fallback_with_young_data_is_stale_not_fresh():
    """Serving good numbers while having lost upstream contact is not 'fresh'.

    This is the case the panel exists for: the data looks fine, but we could
    not reach the source this run, and saying 'fresh' would be a lie.
    """
    result = make_result(
        outcome=Outcome.CACHE_FALLBACK,
        age=timedelta(minutes=30),
        error="Network error: [Errno -2] Name or service not known",
    )
    health = evaluate(result, POLICY, label="Test", now=NOW)

    assert health.state is State.STALE
    assert "unreachable" in health.detail.lower()


def test_not_modified_is_fresh():
    """Upstream saying 'nothing new' is a healthy answer, not a degradation."""
    result = make_result(outcome=Outcome.NOT_MODIFIED, age=timedelta(hours=1))
    assert evaluate(result, POLICY, label="Test", now=NOW).state is State.FRESH


def test_not_modified_reports_both_the_confirmation_and_the_download():
    """ "Fresh" must not hide that the bytes themselves are older.

    Upstream confirming our copy is current makes the *contact* recent, not
    the download. The detail says both, so a reader is never led to think the
    element sets were fetched a minute ago when they were confirmed a minute
    ago and fetched four hours ago.
    """
    result = make_result(outcome=Outcome.NOT_MODIFIED, age=timedelta(minutes=1))
    result.payload_retrieved_at = NOW - timedelta(hours=4)

    health = evaluate(result, POLICY, label="Test", now=NOW)

    assert health.state is State.FRESH
    assert "1m ago" in health.detail
    assert "4h ago" in health.detail


def test_deliberate_cache_hit_is_fresh():
    """Skipping a fetch to respect a rate limit is a healthy state."""
    result = make_result(outcome=Outcome.CACHE_FRESH, age=timedelta(hours=2))
    health = evaluate(result, POLICY, label="Test", now=NOW)

    assert health.state is State.FRESH
    assert "not contacted" in health.detail


def test_data_with_unknown_retrieval_time_is_never_fresh():
    result = make_result(outcome=Outcome.LIVE, age=None)
    health = evaluate(result, POLICY, label="Test", now=NOW)

    assert health.state is State.STALE
    assert "unknown age" in health.detail


def test_fetch_after_the_reference_instant_is_age_zero_not_negative():
    """A source retrieved after `now` is age 0, never negatively old.

    Regression: when the build pinned health classification to its own start
    time, sources fetched during the build landed *after* that instant. The
    resulting negative age rendered as "Fetched from upstream in the future
    ago" and shipped to the live site.
    """
    result = make_result(outcome=Outcome.LIVE, age=-timedelta(minutes=1))
    health = evaluate(result, POLICY, label="Test", now=NOW)

    assert health.state is State.FRESH
    assert health.age_seconds == 0
    assert "future" not in health.detail


def test_stale_by_age_still_reports_lost_contact():
    """Old *and* cut off: the detail must say both, not only the age.

    An operator wants to know contact has been lost, not just that the
    numbers are old -- the second half is the actionable one.
    """
    result = make_result(
        outcome=Outcome.CACHE_FALLBACK, age=timedelta(hours=12), error="HTTP 503"
    )
    health = evaluate(result, POLICY, label="Test", now=NOW)

    assert health.state is State.STALE
    assert "freshness window" in health.detail
    assert "HTTP 503" in health.detail


def test_old_cache_fallback_is_failed_not_merely_stale():
    """Age beats outcome: data too old is FAILED even though we have bytes."""
    result = make_result(
        outcome=Outcome.CACHE_FALLBACK, age=timedelta(days=5), error="timeout"
    )
    assert evaluate(result, POLICY, label="Test", now=NOW).state is State.FAILED


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def _health(state_age: timedelta, outcome=Outcome.LIVE, data=_UNSET):
    return evaluate(
        make_result(age=state_age, outcome=outcome, data=data),
        POLICY,
        label="Test",
        now=NOW,
    )


def test_overall_state_reports_the_worst_source():
    fresh = _health(timedelta(hours=1))
    stale = _health(timedelta(hours=12))
    failed = evaluate(
        make_result(data=None, outcome=Outcome.FAILED, age=None),
        POLICY,
        label="Test",
        now=NOW,
    )

    assert overall_state([fresh, fresh]) is State.FRESH
    assert overall_state([fresh, stale]) is State.STALE
    assert overall_state([fresh, stale, failed]) is State.FAILED
    assert overall_state([]) is State.FRESH


def test_summary_counts_every_state():
    sources = [
        _health(timedelta(hours=1)),
        _health(timedelta(hours=1)),
        _health(timedelta(hours=12)),
    ]
    summary = summarize(sources)

    assert summary["overall"] == "stale"
    assert summary["counts"] == {"fresh": 2, "stale": 1, "failed": 0}
    assert len(summary["sources"]) == 3


def test_health_dict_is_json_serialisable():
    import json

    payload = summarize([_health(timedelta(hours=1))])
    assert json.loads(json.dumps(payload)) == payload


# --------------------------------------------------------------------------
# The configured thresholds follow from the schedule
# --------------------------------------------------------------------------


def test_thresholds_are_derived_from_the_schedule_not_chosen_beside_it():
    """The relations that used to be invariants are now definitions.

    The first design had a cron cadence, a politeness floor and a freshness
    window as three independent numbers, and twice shipped a combination in
    which every component behaved correctly and the site still spent hours
    stale (DECISIONS.md §18, §19). Here the windows are written as
    expressions over the cadence and the upstream facts, so this test is a
    statement of what they mean rather than a guard on hand-tuned values.
    """
    from orbital_watch import config

    # A new document is expected every cadence; three missing is the alarm.
    assert config.PIPELINE_SILENCE_LIMIT == config.REFRESH_CADENCE * 3

    # Celestrak is never asked more often than it publishes.
    assert config.CELESTRAK_MIN_REFETCH_INTERVAL >= config.CELESTRAK_PUBLICATION_INTERVAL

    # In normal running a group's last answer is at most floor + one cadence
    # old, so it is always inside its freshness window with runs to spare...
    worst_normal_age = config.CELESTRAK_MIN_REFETCH_INTERVAL + config.REFRESH_CADENCE
    assert worst_normal_age < config.TLE_STALENESS.fresh_within
    # ...and one failed attempt plus its retry still lands inside it.
    assert (
        config.TLE_STALENESS.fresh_within
        >= config.CELESTRAK_MIN_REFETCH_INTERVAL + config.CELESTRAK_RETRY_INTERVAL
    )

    # Space weather is asked for on every run.
    assert config.SPACE_WEATHER_STALENESS.fresh_within > config.REFRESH_CADENCE

    # The position track outlasts the wait for the next document, and runs out
    # no later than the page starts calling the pipeline silent.
    assert config.EPHEMERIS_WINDOW > config.REFRESH_CADENCE
    assert config.EPHEMERIS_WINDOW <= config.PIPELINE_SILENCE_LIMIT

    # One run finishes before the next starts.
    assert config.REFRESH_CADENCE.total_seconds() > config.RUN_DEADLINE_SECONDS
