"""SGP4 propagation: GP element sets -> positions and passes.

Two questions are answered here, both from the observer's point of view:

* What is above the horizon right now?
* When does each tracked object next come over, and how good is that pass?

Everything is topocentric off a WGS84 site, which needs no planetary
ephemeris -- so the build never downloads a 16 MB JPL kernel and never depends
on network access to do maths.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sgp4 import omm
from sgp4.api import Satrec
from skyfield.api import EarthSatellite, Timescale, load, wgs84
from skyfield.toposlib import GeographicPosition

from orbital_watch.config import (
    HORIZON_ELEVATION_DEG,
    MAX_PASSES_PER_SAT,
    MIN_PASS_ELEVATION_DEG,
    PASS_SEARCH_HOURS,
    Observer,
    SatelliteGroup,
)

log = logging.getLogger(__name__)

#: Skyfield's find_events event codes.
_RISE, _CULMINATE, _SET = 0, 1, 2

_COMPASS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)  # fmt: skip


@dataclass
class TrackedSatellite:
    """An EarthSatellite plus the metadata we report alongside it."""

    name: str
    norad_id: int
    group: str
    satellite: EarthSatellite
    epoch: datetime


@dataclass
class SkyPosition:
    """Where a satellite is right now, as seen from the site."""

    name: str
    norad_id: int
    group: str
    elevation_deg: float
    azimuth_deg: float
    compass: str
    range_km: float
    sub_lat_deg: float
    sub_lon_deg: float
    altitude_km: float
    tle_epoch: datetime


@dataclass
class Pass:
    """A single horizon-to-horizon pass over the site."""

    name: str
    norad_id: int
    group: str
    start: datetime
    peak: datetime
    end: datetime
    peak_elevation_deg: float
    duration_seconds: float
    start_compass: str
    peak_compass: str
    end_compass: str

    @property
    def duration_minutes(self) -> float:
        return self.duration_seconds / 60.0


def timescale() -> Timescale:
    """Skyfield timescale built from bundled data (never hits the network)."""
    return load.timescale(builtin=True)


def site_from(observer: Observer) -> GeographicPosition:
    return wgs84.latlon(
        observer.latitude_deg,
        observer.longitude_deg,
        elevation_m=observer.elevation_m,
    )


def compass_from_azimuth(azimuth_deg: float) -> str:
    """16-point compass label for an azimuth in degrees."""
    index = int((azimuth_deg % 360.0) / 22.5 + 0.5) % 16
    return _COMPASS[index]


def build_satellites(
    records: list[dict], group: SatelliteGroup, ts: Timescale
) -> list[TrackedSatellite]:
    """Turn GP records into propagatable satellites.

    A record that SGP4 rejects is dropped with a warning rather than aborting
    the group -- one bad element set should cost us one satellite, not the
    whole build.
    """
    tracked: list[TrackedSatellite] = []
    for record in records:
        try:
            satrec = Satrec()
            omm.initialize(satrec, record)
            sat = EarthSatellite.from_satrec(satrec, ts)
            # from_satrec does not carry OBJECT_NAME across; set it ourselves
            # so downstream output is human-readable.
            name = str(record.get("OBJECT_NAME", "")).strip() or f"NORAD {satrec.satnum}"
            sat.name = name
            tracked.append(
                TrackedSatellite(
                    name=name,
                    norad_id=int(record["NORAD_CAT_ID"]),
                    group=group.key,
                    satellite=sat,
                    epoch=sat.epoch.utc_datetime(),
                )
            )
        except Exception as exc:
            log.warning(
                "skipping unpropagatable object group=%s name=%r error=%s",
                group.key,
                record.get("OBJECT_NAME"),
                exc,
            )
    log.info("group=%s built %d/%d satellites", group.key, len(tracked), len(records))
    return tracked


def sky_view(
    tracked: list[TrackedSatellite],
    site: GeographicPosition,
    ts: Timescale,
    when: datetime,
    *,
    min_elevation_deg: float = HORIZON_ELEVATION_DEG,
) -> list[SkyPosition]:
    """Satellites currently above ``min_elevation_deg``, highest first."""
    t = ts.from_datetime(_as_utc(when))
    above: list[SkyPosition] = []

    for item in tracked:
        try:
            topocentric = (item.satellite - site).at(t)
            alt, az, distance = topocentric.altaz()
            if alt.degrees < min_elevation_deg:
                continue
            subpoint = item.satellite.at(t).subpoint()
            above.append(
                SkyPosition(
                    name=item.name,
                    norad_id=item.norad_id,
                    group=item.group,
                    elevation_deg=float(alt.degrees),
                    azimuth_deg=float(az.degrees),
                    compass=compass_from_azimuth(float(az.degrees)),
                    range_km=float(distance.km),
                    sub_lat_deg=float(subpoint.latitude.degrees),
                    sub_lon_deg=float(subpoint.longitude.degrees),
                    altitude_km=float(subpoint.elevation.km),
                    tle_epoch=item.epoch,
                )
            )
        except Exception as exc:
            log.warning("sky_view failed for %s: %s", item.name, exc)

    above.sort(key=lambda p: p.elevation_deg, reverse=True)
    return above


def next_passes(
    tracked: list[TrackedSatellite],
    site: GeographicPosition,
    ts: Timescale,
    start: datetime,
    *,
    hours: int = PASS_SEARCH_HOURS,
    min_elevation_deg: float = MIN_PASS_ELEVATION_DEG,
    max_per_satellite: int = MAX_PASSES_PER_SAT,
) -> list[Pass]:
    """Upcoming passes over the site, soonest first."""
    begin = _as_utc(start)
    t0 = ts.from_datetime(begin)
    t1 = ts.from_datetime(begin + timedelta(hours=hours))

    passes: list[Pass] = []
    for item in tracked:
        try:
            times, events = item.satellite.find_events(
                site, t0, t1, altitude_degrees=min_elevation_deg
            )
        except Exception as exc:
            log.warning("pass search failed for %s: %s", item.name, exc)
            continue

        found = _assemble_passes(item, site, times, events, max_per_satellite)
        passes.extend(found)

    passes.sort(key=lambda p: p.start)
    return passes


def _assemble_passes(
    item: TrackedSatellite,
    site: GeographicPosition,
    times,
    events,
    max_per_satellite: int,
) -> list[Pass]:
    """Group rise/culminate/set events into complete passes.

    find_events can hand back a partial sequence when the search window cuts
    across a pass -- a set with no rise, or a rise with no set. We only emit
    complete rise-culminate-set triples so that every reported pass has a
    real start, peak, and end rather than an invented one.
    """
    collected: list[Pass] = []
    pending: dict[int, object] = {}

    for t, event in zip(times, events, strict=False):
        event = int(event)
        if event == _RISE:
            pending = {_RISE: t}
        elif event == _CULMINATE:
            if _RISE in pending:
                pending[_CULMINATE] = t
        elif event == _SET:
            if _RISE in pending and _CULMINATE in pending:
                built = _build_pass(item, site, pending[_RISE], pending[_CULMINATE], t)
                if built is not None:
                    collected.append(built)
                    if len(collected) >= max_per_satellite:
                        break
            pending = {}

    return collected


def _build_pass(
    item: TrackedSatellite, site: GeographicPosition, t_rise, t_peak, t_set
) -> Pass | None:
    try:
        difference = item.satellite - site
        peak_alt, peak_az, _ = difference.at(t_peak).altaz()
        _, rise_az, _ = difference.at(t_rise).altaz()
        _, set_az, _ = difference.at(t_set).altaz()

        start = t_rise.utc_datetime()
        end = t_set.utc_datetime()
        return Pass(
            name=item.name,
            norad_id=item.norad_id,
            group=item.group,
            start=start,
            peak=t_peak.utc_datetime(),
            end=end,
            peak_elevation_deg=float(peak_alt.degrees),
            duration_seconds=(end - start).total_seconds(),
            start_compass=compass_from_azimuth(float(rise_az.degrees)),
            peak_compass=compass_from_azimuth(float(peak_az.degrees)),
            end_compass=compass_from_azimuth(float(set_az.degrees)),
        )
    except Exception as exc:
        log.warning("could not build pass for %s: %s", item.name, exc)
        return None


def _as_utc(when: datetime) -> datetime:
    return when if when.tzinfo else when.replace(tzinfo=UTC)
