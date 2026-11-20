"""Celestrak GP (general perturbations) element sets.

We pull named groups rather than the full catalogue. Celestrak rate-limits
abusive clients and blocks them outright, so being a well-behaved consumer of
a free public service is a requirement, not a nicety.

How often each group may be asked is not decided here or by the job's
schedule. It is a property of the source (:func:`group_spec`), and
:func:`orbital_watch.sources.fetch.fetch_source` enforces it against the
stored record of the last attempt. The job can run every ten minutes, or be
started by hand five times in a row, and Celestrak still sees one request per
group per publication interval.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import urlencode

from orbital_watch.config import (
    CELESTRAK_GP_URL,
    CELESTRAK_MIN_REFETCH_INTERVAL,
    CELESTRAK_RETRY_INTERVAL,
    SatelliteGroup,
)
from orbital_watch.sources.fetch import Payload, SourceSpec

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

#: A newly fetched group must keep at least this fraction of the object count
#: we already hold, once that count is big enough for the ratio to mean
#: anything. Guards against a truncated-but-valid response silently replacing
#: a full group.
SHRINKAGE_FLOOR = 0.5
MIN_PLAUSIBLE_RECORDS = 10


def source_name(group: SatelliteGroup) -> str:
    return f"celestrak_{group.key}"


def group_url(group_key: str) -> str:
    """Build the GP API URL for a named group."""
    query = urlencode({"GROUP": group_key, "FORMAT": "json"})
    return f"{CELESTRAK_GP_URL}?{query}"


def validate_gp_records(data: object) -> list[dict]:
    """Reject anything that isn't a usable list of GP records.

    Raising :class:`ValueError` here routes through the fetch helper's
    fallback, so a garbage upstream response degrades to stored data instead
    of replacing it.
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


def select_objects(records: list[dict], group: SatelliteGroup) -> list[dict]:
    """Trim a group down to the objects we will actually propagate.

    Starlink ships ~11k objects. Propagating all of them would dominate run
    time and produce a document no browser should be asked to parse, for no
    analytical gain -- the point of the dashboard is representative
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


def _refuse_collapse(new: list[dict], previous: list[dict]) -> None:
    """Reject a payload that validates but has collapsed in size.

    Two stations where we hold twenty-two is upstream trouble, not a real
    change: constellations do not lose most of their objects between two
    publications. Refusing it keeps the stored group, and if the shrinkage is
    real and persists, the group's age tells the story.
    """
    if (
        len(previous) >= MIN_PLAUSIBLE_RECORDS
        and len(new) < len(previous) * SHRINKAGE_FLOOR
    ):
        raise ValueError(
            f"group shrank from {len(previous)} to {len(new)} objects, below the "
            f"{int(SHRINKAGE_FLOOR * 100)}% floor; keeping the stored group"
        )


def group_spec(group: SatelliteGroup) -> SourceSpec:
    """Describe one GP group to the fetch helper.

    What is kept in the state store is the *trimmed* group -- the objects we
    propagate -- because the full Starlink group is about 5 MB and would be
    rewritten every two hours for nothing. The size of the group before
    trimming is recorded as a fact alongside it, since the page states its own
    sampling ("60 of 11,205 starlink") and that number is not recoverable from
    the sixty records kept.
    """

    def parse(data: object) -> Payload:
        records = validate_gp_records(data)
        return Payload(
            data=select_objects(records, group),
            facts={"available": len(records)},
        )

    return SourceSpec(
        name=source_name(group),
        url=group_url(group.key),
        parse=parse,
        revalidate=validate_gp_records,
        plausible=_refuse_collapse,
        min_refetch_interval=CELESTRAK_MIN_REFETCH_INTERVAL,
        retry_interval=CELESTRAK_RETRY_INTERVAL,
    )
