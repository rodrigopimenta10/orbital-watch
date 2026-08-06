"""NOAA Space Weather Prediction Center products.

Three independent reads (Kp, solar wind, solar cycle) rather than one, so that
losing one does not cost us the others -- the space weather panel can render
with a Kp index and no solar wind, and says so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from orbital_watch.config import (
    KP_ELEVATED_THRESHOLD,
    KP_STORM_THRESHOLD,
    SWPC_KP_URL,
    SWPC_MAG_FIELD_URL,
    SWPC_SOLAR_CYCLE_URL,
    SWPC_SOLAR_WIND_URL,
)
from orbital_watch.sources.fetch import FetchResult, fetch_json

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Fetchers
# --------------------------------------------------------------------------


def _validate_nonempty_list(data: object) -> list:
    if not isinstance(data, list):
        raise ValueError(f"expected a list, got {type(data).__name__}")
    if not data:
        raise ValueError("payload contained no records")
    return data


def fetch_kp(cache_dir: Path) -> FetchResult:
    """Planetary K-index, 3-hourly, roughly a week of history."""
    return fetch_json(
        name="swpc_kp",
        url=SWPC_KP_URL,
        cache_dir=cache_dir,
        parser=_validate_nonempty_list,
    )


def fetch_solar_wind(cache_dir: Path) -> FetchResult:
    """Current solar wind bulk speed."""
    return fetch_json(
        name="swpc_solar_wind",
        url=SWPC_SOLAR_WIND_URL,
        cache_dir=cache_dir,
        parser=_validate_nonempty_list,
    )


def fetch_mag_field(cache_dir: Path) -> FetchResult:
    """Interplanetary magnetic field: total (Bt) and north-south (Bz) in GSM."""
    return fetch_json(
        name="swpc_mag_field",
        url=SWPC_MAG_FIELD_URL,
        cache_dir=cache_dir,
        parser=_validate_nonempty_list,
    )


def fetch_solar_cycle(cache_dir: Path) -> FetchResult:
    """Observed solar cycle indices; we only use the recent tail (F10.7, SSN)."""

    def _tail(data: object) -> list:
        records = _validate_nonempty_list(data)
        return records[-36:]

    return fetch_json(
        name="swpc_solar_cycle",
        url=SWPC_SOLAR_CYCLE_URL,
        cache_dir=cache_dir,
        parser=_tail,
    )


# --------------------------------------------------------------------------
# Interpretation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class KpReading:
    time_tag: datetime
    kp: float


#: NOAA G-scale, keyed by the floor of Kp.
_KP_BANDS: tuple[tuple[float, str, str], ...] = (
    (9.0, "extreme", "G5 — extreme storm"),
    (8.0, "severe", "G4 — severe storm"),
    (7.0, "strong", "G3 — strong storm"),
    (6.0, "moderate", "G2 — moderate storm"),
    (5.0, "minor storm", "G1 — minor storm"),
    (4.0, "active", "Active, below storm threshold"),
    (3.0, "unsettled", "Unsettled"),
    (0.0, "quiet", "Quiet"),
)


def classify_kp(kp: float) -> tuple[str, str]:
    """Map a Kp value to (label, plain-English description)."""
    for floor, label, description in _KP_BANDS:
        if kp >= floor:
            return label, description
    return "quiet", "Quiet"


def parse_kp_series(records: list) -> list[KpReading]:
    """Normalise the Kp product into a clean time series.

    SWPC has shipped this product in two shapes over the years: a list of
    dicts, and a list-of-lists with a header row. We accept both rather than
    assume, because the shape changing upstream should degrade to "we parsed
    what we could" and not a build failure.
    """
    # Guard the container itself, not just its contents. A cached payload
    # written by an older revision could be a dict or a scalar, and
    # records[0] on a dict raises KeyError rather than returning an item.
    if not isinstance(records, list):
        log.warning(
            "kp payload is %s, expected a list; parsed nothing",
            type(records).__name__,
        )
        return []

    readings: list[KpReading] = []

    if records and isinstance(records[0], dict):
        for row in records:
            parsed = _reading_from(row.get("time_tag"), row.get("Kp"))
            if parsed:
                readings.append(parsed)
    elif records and isinstance(records[0], list):
        header = [str(cell).strip().lower() for cell in records[0]]
        try:
            time_idx = header.index("time_tag")
            kp_idx = next(
                i for i, name in enumerate(header) if name in ("kp", "kp_index")
            )
        except (ValueError, StopIteration):
            log.warning("kp payload header not recognised: %s", header)
            return []
        for row in records[1:]:
            if not isinstance(row, list | tuple) or len(row) <= max(time_idx, kp_idx):
                continue
            parsed = _reading_from(row[time_idx], row[kp_idx])
            if parsed:
                readings.append(parsed)

    readings.sort(key=lambda r: r.time_tag)
    return readings


def _reading_from(raw_time: object, raw_kp: object) -> KpReading | None:
    if raw_time is None or raw_kp is None:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    try:
        kp = float(raw_kp)
    except (TypeError, ValueError):
        return None
    return KpReading(time_tag=stamp, kp=kp)


def kp_trend(readings: list[KpReading], window: int = 4) -> str:
    """Describe the recent direction of Kp: rising / falling / steady."""
    if len(readings) < 2:
        return "unknown"
    recent = readings[-window:]
    delta = recent[-1].kp - recent[0].kp
    if delta >= 1.0:
        return "rising"
    if delta <= -1.0:
        return "falling"
    return "steady"


def is_elevated(kp: float) -> bool:
    """Whether Kp warrants the operational correlation banner."""
    return kp >= KP_ELEVATED_THRESHOLD


def is_storm(kp: float) -> bool:
    return kp >= KP_STORM_THRESHOLD


def latest_value(records: list, *keys: str) -> tuple[float | None, datetime | None]:
    """Pull the most recent numeric value for the first matching key.

    Used for the summary endpoints, which return a single-element list of
    dicts, e.g. ``[{"proton_speed": 301, "time_tag": "..."}]``.
    """
    if not isinstance(records, list) or not records:
        return None, None
    row = records[-1]
    if not isinstance(row, dict):
        return None, None

    value: float | None = None
    for key in keys:
        if row.get(key) is not None:
            try:
                value = float(row[key])
            except (TypeError, ValueError):
                value = None
            break

    stamp: datetime | None = None
    raw_time = row.get("time_tag")
    if raw_time:
        try:
            stamp = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
        except ValueError:
            stamp = None
    return value, stamp
