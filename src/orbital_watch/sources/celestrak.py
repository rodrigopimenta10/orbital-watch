"""Celestrak GP (general perturbations) element sets.

We pull named groups rather than the full catalogue, cache aggressively, and
refuse to re-fetch a group whose cache is younger than
:data:`~orbital_watch.config.CELESTRAK_MIN_REFETCH_INTERVAL`. Celestrak
rate-limits abusive clients and blocks them outright; being a well-behaved
consumer of a free public service is a requirement, not a nicety.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from orbital_watch.config import (
    CELESTRAK_GP_URL,
    CELESTRAK_MIN_REFETCH_INTERVAL,
    SatelliteGroup,
)
from orbital_watch.sources.fetch import FetchResult, fetch_json

log = logging.getLogger(__name__)

#: Fields we require on every GP record before we will hand it to SGP4.
#: A record missing any of these cannot be propagated, so it is dropped at the
#: boundary rather than exploding somewhere downstream.
REQUIRED_GP_FIELDS = (
    "OBJECT_NAME",
    "NORAD_CAT_ID",
    "EPOCH",
    "MEAN_MOTION",
    "ECCENTRICITY",
    "INCLINATION",
    "RA_OF_ASC_NODE",
    "ARG_OF_PERICENTER",
    "MEAN_ANOMALY",
    "BSTAR",
)


def group_url(group_key: str) -> str:
    """Build the GP API URL for a named group."""
    query = urlencode({"GROUP": group_key, "FORMAT": "json"})
    return f"{CELESTRAK_GP_URL}?{query}"


def _validate_gp_payload(data: object) -> list[dict]:
    """Reject anything that isn't a usable list of GP records.

    Raising :class:`ValueError` here routes through the fetch helper's cache
    fallback, so a garbage upstream response degrades to cached data instead
    of poisoning the cache.
    """
    if not isinstance(data, list):
        raise ValueError(f"expected a list of GP records, got {type(data).__name__}")
    if not data:
        raise ValueError("GP payload contained no records")

    usable = [
        record
        for record in data
        if isinstance(record, dict)
        and all(record.get(field) is not None for field in REQUIRED_GP_FIELDS)
    ]
    if not usable:
        raise ValueError(
            f"GP payload had {len(data)} records but none carried all required "
            f"fields {REQUIRED_GP_FIELDS}"
        )
    if len(usable) < len(data):
        log.warning(
            "dropped %d GP record(s) missing required fields", len(data) - len(usable)
        )
    return usable


def fetch_group(group: SatelliteGroup, cache_dir: Path) -> FetchResult:
    """Fetch one Celestrak GP group, cached and rate-limit-aware."""
    return fetch_json(
        name=f"celestrak_{group.key}",
        url=group_url(group.key),
        cache_dir=cache_dir,
        min_refetch_interval=CELESTRAK_MIN_REFETCH_INTERVAL,
        parser=_validate_gp_payload,
    )


def select_objects(records: list[dict], group: SatelliteGroup) -> list[dict]:
    """Trim a group down to the objects we will actually propagate.

    Starlink ships ~11k objects. Propagating all of them would dominate build
    time and produce an output file no browser should be asked to parse, for
    no analytical gain -- the point of the dashboard is representative
    visibility, not a catalogue browser (explicitly out of scope, brief §4.2).

    We keep the objects with the most recent epochs, since those have the
    least propagation error.
    """
    if len(records) <= group.max_objects:
        return list(records)

    ranked = sorted(records, key=_epoch_sort_key, reverse=True)
    trimmed = ranked[: group.max_objects]
    log.info(
        "group=%s trimmed %d objects to %d (most recent epochs)",
        group.key,
        len(records),
        len(trimmed),
    )
    return trimmed


def _epoch_sort_key(record: dict) -> datetime:
    raw = record.get("EPOCH")
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
