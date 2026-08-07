"""The cron/floor/freshness interaction, pinned.

Production bug, 2026-08-07: with the refresher cron at 6 hours and the
politeness floor at 6 hours, every other run landed minutes under the floor
and correctly skipped — so the effective refresh interval was ~12 hours
against an 8-hour freshness window, and the site spent hours STALE every
cycle. Every individual component behaved exactly as designed and as tested;
the bug lived only in their composition. These tests cover the composition.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from orbital_watch.config import (
    CELESTRAK_MIN_REFETCH_INTERVAL,
    SNAPSHOT_REFRESH_CADENCE,
    SNAPSHOT_SCHEDULING_MARGIN,
    TLE_STALENESS,
)

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "refresh-snapshot.yml"


# --------------------------------------------------------------------------
# The invariant itself
# --------------------------------------------------------------------------


def test_cadence_floor_and_freshness_compose():
    """floor + cadence must fit inside the freshness window, with cadence < floor.

    This is the constraint whose violation shipped: cadence == floor makes
    every other tick land just under the floor and skip, doubling the
    effective interval. cadence strictly under floor guarantees some tick
    always lands within one cadence of the floor clearing, so snapshot age
    is bounded by floor + cadence — which must not exceed the freshness
    window, or the bound is met and the site still reports STALE.
    """
    assert SNAPSHOT_REFRESH_CADENCE < CELESTRAK_MIN_REFETCH_INTERVAL, (
        "cadence must be strictly shorter than the politeness floor; equal "
        "values are exactly the production bug"
    )
    # Strict, with an explicit margin. Simulation showed floor + cadence ==
    # fresh_within converges to a refresh gap of exactly the window edge:
    # each capture lands moments after a tick, so the floor clears moments
    # after the tick at floor-age, which skips -- and the refresh slides to
    # the next tick, every cycle, forever. The margin absorbs that run-time
    # offset plus routine Actions cron lateness.
    assert (
        TLE_STALENESS.fresh_within
        >= CELESTRAK_MIN_REFETCH_INTERVAL
        + SNAPSHOT_REFRESH_CADENCE
        + SNAPSHOT_SCHEDULING_MARGIN
    ), (
        "floor + cadence + margin must fit the freshness window; "
        "the floor is DERIVED from this -- see config.py"
    )


# --------------------------------------------------------------------------
# Simulation: the property §11 says no test covered
# --------------------------------------------------------------------------


def simulate_refresh_gaps(
    cadence: timedelta,
    floor: timedelta,
    *,
    run_duration: timedelta = timedelta(seconds=60),
    cycles: int = 40,
) -> list[timedelta]:
    """Gaps between successive successful refreshes under cron + floor rules.

    Models exactly what runs in production: a tick fires every ``cadence``;
    the refresher captures only when the snapshot's age has reached ``floor``,
    and a capture lands ``run_duration`` after its tick (fetch + commit time —
    the offset that makes the equal-cadence case land *just* under the floor
    on every other tick).
    """
    gaps: list[timedelta] = []
    tick = timedelta(0)
    last_capture = tick + run_duration  # initial capture on the first tick
    while len(gaps) < cycles:
        tick += cadence
        age_at_tick = tick - last_capture
        if age_at_tick >= floor:
            capture = tick + run_duration
            gaps.append(capture - last_capture)
            last_capture = capture
    return gaps


def test_successive_refresh_gap_stays_inside_the_freshness_window():
    """For any snapshot age, refresh-to-refresh gaps stay < fresh_within.

    The property that was violated in production, asserted directly against
    the real configured values.
    """
    gaps = simulate_refresh_gaps(SNAPSHOT_REFRESH_CADENCE, CELESTRAK_MIN_REFETCH_INTERVAL)
    worst = max(gaps)
    assert worst < TLE_STALENESS.fresh_within, (
        f"worst refresh gap {worst} breaches the "
        f"{TLE_STALENESS.fresh_within} freshness window"
    )


def test_the_old_equal_cadence_config_violates_the_property():
    """The pre-fix configuration must fail this model — proving it catches
    the actual bug rather than passing vacuously.

    Cron every 6h with a 6h floor: each tick lands run_duration short of the
    floor, skips, and the next tick refreshes at ~12h — deterministically,
    not as a race.
    """
    gaps = simulate_refresh_gaps(cadence=timedelta(hours=6), floor=timedelta(hours=6))
    worst = max(gaps)
    assert worst > TLE_STALENESS.fresh_within, (
        "the simulation no longer reproduces the production bug; "
        "it has stopped testing anything"
    )
    # And specifically the observed shape: ~2x the floor.
    assert worst >= timedelta(hours=12)


# --------------------------------------------------------------------------
# The YAML must match the constant, or the invariant tests test fiction
# --------------------------------------------------------------------------


def cron_hour_gaps(cron: str) -> list[int]:
    """Gaps in hours between consecutive daily ticks of an hour-field cron."""
    fields = cron.split()
    assert len(fields) == 5, f"unexpected cron shape: {cron!r}"
    hour_field = fields[1]

    hours: set[int] = set()
    for part in hour_field.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/")
            step = int(step_text)
        if part == "*":
            lo, hi = 0, 23
        elif "-" in part:
            lo_text, hi_text = part.split("-")
            lo, hi = int(lo_text), int(hi_text)
        else:
            lo = hi = int(part)
        hours.update(range(lo, hi + 1, step))

    ordered = sorted(hours)
    assert ordered, f"cron hour field parsed to nothing: {hour_field!r}"
    return [
        (ordered[(i + 1) % len(ordered)] - h) % 24 or 24 for i, h in enumerate(ordered)
    ]


def test_workflow_cron_matches_the_configured_cadence():
    """refresh-snapshot.yml's schedule must agree with SNAPSHOT_REFRESH_CADENCE.

    The invariant tests above reason about the constant; if the workflow's
    cron drifts from it, they start verifying a configuration that is not the
    one running. This pins the two together, so changing either alone fails
    CI with a pointer to the other.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text())
    triggers = workflow.get("on", workflow.get(True))
    cron = triggers["schedule"][0]["cron"]

    gaps = cron_hour_gaps(cron)
    expected_hours = SNAPSHOT_REFRESH_CADENCE.total_seconds() / 3600
    assert all(gap == expected_hours for gap in gaps), (
        f"cron {cron!r} ticks at gaps {sorted(set(gaps))}h but "
        f"SNAPSHOT_REFRESH_CADENCE says {expected_hours}h — update whichever "
        f"one is wrong, and re-check the invariant in "
        f"test_cadence_floor_and_freshness_compose"
    )


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("30 1-23/2 * * *", [2] * 12),
        ("30 1,7,13,19 * * *", [6, 6, 6, 6]),  # the old, buggy schedule
        ("0 */2 * * *", [2] * 12),
        ("0 3 * * *", [24]),
    ],
)
def test_cron_gap_parser(field, expected):
    """The helper itself, pinned against known schedules."""
    assert cron_hour_gaps(field) == expected
