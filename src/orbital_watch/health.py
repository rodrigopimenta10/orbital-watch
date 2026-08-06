"""Per-source freshness computation.

The health panel is a first-class feature, so this module has one job and does
it honestly: given what actually happened during a fetch, decide whether that
source is FRESH, STALE, or FAILED, and explain the decision in terms the
viewer can check against the published thresholds.

The rule, in order of precedence:

1. No data at all -> FAILED, regardless of age.
2. Otherwise the state is driven by the age of the payload we are serving,
   measured against the source's own :class:`StalenessPolicy`.
3. A fetch that failed but fell back to a *young* cache is still STALE at
   worst -- we are serving good data, but we have lost contact and the viewer
   deserves to know that.

Point 3 is the interesting one. A source can be serving perfectly good numbers
while being broken. Reporting it as "fresh" because the numbers look fine
would be a lie of exactly the kind this panel exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from orbital_watch.config import StalenessPolicy
from orbital_watch.sources.fetch import FetchResult, Outcome, format_duration


class State(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    FAILED = "failed"


@dataclass
class SourceHealth:
    """Reportable health for one upstream source."""

    name: str
    label: str
    url: str
    state: State
    outcome: Outcome
    last_success: datetime | None
    age_seconds: float | None
    fresh_within_seconds: float
    stale_within_seconds: float
    detail: str
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "url": self.url,
            "state": self.state.value,
            "outcome": self.outcome.value,
            "last_success": (
                self.last_success.isoformat() if self.last_success else None
            ),
            "age_seconds": self.age_seconds,
            "thresholds": {
                "fresh_within_seconds": self.fresh_within_seconds,
                "stale_within_seconds": self.stale_within_seconds,
            },
            "detail": self.detail,
            "error": self.error,
        }


def evaluate(
    result: FetchResult,
    policy: StalenessPolicy,
    *,
    label: str,
    now: datetime | None = None,
) -> SourceHealth:
    """Compute health for one source.

    Args:
        result: What the fetch helper reported.
        policy: The freshness thresholds for this source.
        label: Human-readable source name for the panel.
        now: Injectable clock for testing.
    """
    now = now or datetime.now(UTC)

    state, age, detail = _classify(result, policy, now)

    return SourceHealth(
        name=result.name,
        label=label,
        url=result.url,
        state=state,
        outcome=result.outcome,
        last_success=result.last_success,
        age_seconds=age.total_seconds() if age is not None else None,
        fresh_within_seconds=policy.fresh_within.total_seconds(),
        stale_within_seconds=policy.stale_within.total_seconds(),
        detail=detail,
        error=result.error,
    )


def _classify(
    result: FetchResult, policy: StalenessPolicy, now: datetime
) -> tuple[State, timedelta | None, str]:
    # 1. No data is unambiguous.
    if result.data is None:
        return (
            State.FAILED,
            None,
            result.error or "No data available and no cache to fall back on.",
        )

    # 2. Data with no known retrieval time. We are serving something we cannot
    #    date, which we refuse to call fresh.
    if result.last_success is None:
        return (
            State.STALE,
            None,
            "Serving data of unknown age; retrieval timestamp is missing.",
        )

    age = now - result.last_success
    age_text = format_duration(age)

    # Data can be both old *and* cut off from upstream. Reporting only the age
    # would hide the more actionable half -- an operator wants to know we have
    # lost contact, not just that the numbers are old.
    contact_lost = (
        f" Upstream was also unreachable this run ({result.error})."
        if result.outcome == Outcome.CACHE_FALLBACK
        else ""
    )

    # 3. Age-driven classification.
    if age > policy.stale_within:
        return (
            State.FAILED,
            age,
            f"Last successful fetch was {age_text} ago, beyond the "
            f"{format_duration(policy.stale_within)} limit. Data shown is not "
            f"operationally current.{contact_lost}",
        )

    if age > policy.fresh_within:
        return (
            State.STALE,
            age,
            f"Last successful fetch was {age_text} ago, past the "
            f"{format_duration(policy.fresh_within)} freshness window."
            f"{contact_lost}",
        )

    # 4. Young data, but did we actually reach upstream this run?
    if result.outcome == Outcome.CACHE_FALLBACK:
        return (
            State.STALE,
            age,
            f"Upstream unreachable this run ({result.error}); serving cached "
            f"data from {age_text} ago.",
        )

    if result.outcome == Outcome.NOT_MODIFIED:
        return (
            State.FRESH,
            age,
            f"Upstream reports no new data since our last download {age_text} ago.",
        )

    if result.outcome == Outcome.CACHE_FRESH:
        return (
            State.FRESH,
            age,
            f"Cache is {age_text} old and within the minimum refetch interval; "
            f"upstream was deliberately not contacted.",
        )

    return State.FRESH, age, f"Fetched from upstream {age_text} ago."


def overall_state(sources: list[SourceHealth]) -> State:
    """Worst state across all sources -- what the top-level badge shows."""
    if any(s.state == State.FAILED for s in sources):
        return State.FAILED
    if any(s.state == State.STALE for s in sources):
        return State.STALE
    return State.FRESH


def summarize(sources: list[SourceHealth]) -> dict:
    """Health block for the output JSON."""
    return {
        "overall": overall_state(sources).value,
        "counts": {
            state.value: sum(1 for s in sources if s.state == state) for state in State
        },
        "sources": [s.to_dict() for s in sources],
    }
