"""SGP4 propagation: GP element sets -> positions, tracks and passes.

Three questions are answered here, all from the observer's point of view:

* What is above the horizon at a given instant?
* Where will each nearby object be over the next half hour? (The page shows
  the sky at the moment it is *viewed*, which is later than the moment this
  ran, so it is given a short track to interpolate along.)
* When does each tracked object next come over, and how good is that pass?

Everything is topocentric off a WGS84 site, which needs no planetary
ephemeris -- so the job never downloads a 16 MB JPL kernel and never depends
on network access to do maths.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sgp4 import omm
from sgp4.api import Satrec
from skyfield.api import EarthSatellite, Timescale, load, wgs84
from skyfield.toposlib import GeographicPosition

from orbital_watch.config import (
    EPHEMERIS_HORIZON_MARGIN_DEG,
    EPHEMERIS_STEP,
    EPHEMERIS_WINDOW,
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
class Track:
    """Where one satellite is, sample by sample, as seen from the site.

    Each sample is ``(east_km, north_km, up_km, altitude_km)``: the satellite's
    position relative to the site in the local east/north/up frame, plus its
    height above the ellipsoid. See :func:`ephemeris` for why the track is a
    vector and not elevation and azimuth.
    """

    name: str
    norad_id: int
    group: str
    tle_epoch: datetime
    samples: list[tuple[float, float, float, float]]


@dataclass
class PassSearch:
    """Result of a pass search, including whether it ran to completion."""

    passes: list[Pass]
    #: Satellites actually searched, out of ``total``. Fewer means the search
    #: was stopped at the run deadline and the list is incomplete.
    searched: int
    total: int

    @property
    def complete(self) -> bool:
        return self.searched >= self.total


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
    whole run.
    """
    tracked: list[TrackedSatellite] = []
    for record in records:
        try:
            if not isinstance(record, dict):
                raise TypeError(f"expected a GP record dict, got {type(record).__name__}")
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
            # Deliberately avoids record.get() here. This handler exists to
            # absorb malformed records, so calling a dict method on one would
            # make the handler itself raise -- turning "skip one satellite"
            # into "abort the stage", the exact opposite of its purpose.
            label = (
                record.get("OBJECT_NAME")
                if isinstance(record, dict)
                else repr(record)[:60]
            )
            log.warning(
                "skipping unpropagatable object group=%s name=%r error=%s",
                group.key,
                label,
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


def ephemeris(
    tracked: list[TrackedSatellite],
    site: GeographicPosition,
    ts: Timescale,
    start: datetime,
    *,
    window: timedelta = EPHEMERIS_WINDOW,
    step: timedelta = EPHEMERIS_STEP,
    min_elevation_deg: float = HORIZON_ELEVATION_DEG - EPHEMERIS_HORIZON_MARGIN_DEG,
) -> list[Track]:
    """Short position tracks for every object near the sky during ``window``.

    The page is read at some later moment than this runs, and a satellite in
    low orbit crosses the whole sky in about ten minutes. Positions computed
    once and shown as "now" would be wrong almost immediately. So instead of
    one position per object, this publishes a track sampled every ``step``
    across ``window``, and the browser interpolates along it to its own clock.
    All the orbital mechanics stays here; the browser draws a straight line
    between two points.

    The samples are east/north/up vectors, not elevation and azimuth, because
    of what that straight line does. Over one step a satellite's path is very
    nearly straight in space, so interpolating the *vector* is accurate to
    about a kilometre. Elevation and azimuth are not nearly linear: a pass
    through the zenith swings the azimuth by half a turn in seconds and peaks
    the elevation between two samples, and interpolating those angles directly
    would cut the top off the pass. The browser turns the interpolated vector
    into angles with two arctangents.

    Only objects that reach ``min_elevation_deg`` at some sample are included.
    That threshold sits slightly below the horizon so the sample just before a
    rise and just after a set are always there to interpolate between.
    """
    begin = _as_utc(start)
    count = int(window / step) + 1
    times = ts.from_datetimes([begin + step * index for index in range(count)])

    tracks: list[Track] = []
    for item in tracked:
        try:
            alt, az, distance = (item.satellite - site).at(times).altaz()
            elevations = alt.radians.tolist()
            azimuths = az.radians.tolist()
            ranges = distance.km.tolist()
            heights = wgs84.height_of(item.satellite.at(times)).km.tolist()

            # SGP4 reports an element set it cannot propagate (a decayed
            # object, say) as NaN positions rather than an exception. One such
            # object must cost one track, and a NaN must never reach the
            # document: JSON has no way to write it.
            if not all(
                math.isfinite(value)
                for series in (elevations, azimuths, ranges, heights)
                for value in series
            ):
                raise ValueError("propagation produced non-finite positions")

            if math.degrees(max(elevations)) < min_elevation_deg:
                continue

            samples = []
            for el, azimuth, rng, height in zip(
                elevations, azimuths, ranges, heights, strict=True
            ):
                horizontal = rng * math.cos(el)
                samples.append(
                    (
                        round(horizontal * math.sin(azimuth), 1),
                        round(horizontal * math.cos(azimuth), 1),
                        round(rng * math.sin(el), 1),
                        round(height, 1),
                    )
                )
            tracks.append(
                Track(
                    name=item.name,
                    norad_id=item.norad_id,
                    group=item.group,
                    tle_epoch=item.epoch,
                    samples=samples,
                )
            )
        except Exception as exc:
            log.warning("ephemeris failed for %s: %s", item.name, exc)

    return tracks


def position_from_track(
    samples: list[tuple[float, float, float, float]],
    start: datetime,
    step: timedelta,
    when: datetime,
) -> tuple[float, float, float] | None:
    """Elevation, azimuth (degrees) and range (km) at ``when``, from a track.

    This is the arithmetic the browser does, written once more in Python so
    the tests can hold the published track to its accuracy claim against SGP4
    itself. Returns ``None`` outside the track.
    """
    offset = (_as_utc(when) - _as_utc(start)) / step
    if offset < 0 or offset > len(samples) - 1:
        return None
    index = min(int(offset), len(samples) - 2)
    fraction = offset - index
    east, north, up = (
        a + (b - a) * fraction
        for a, b in zip(samples[index][:3], samples[index + 1][:3], strict=True)
    )
    rng = math.sqrt(east * east + north * north + up * up)
    elevation = math.degrees(math.atan2(up, math.hypot(east, north)))
    azimuth = math.degrees(math.atan2(east, north)) % 360.0
    return elevation, azimuth, rng


def search_passes(
    tracked: list[TrackedSatellite],
    site: GeographicPosition,
    ts: Timescale,
    start: datetime,
    *,
    hours: int = PASS_SEARCH_HOURS,
    min_elevation_deg: float = MIN_PASS_ELEVATION_DEG,
    max_per_satellite: int = MAX_PASSES_PER_SAT,
    out_of_time: Callable[[], bool] | None = None,
) -> PassSearch:
    """Upcoming passes over the site, soonest first.

    The search is the slow part of a run -- a root-find per satellite over a
    day -- so it is the one computation that checks the run deadline. If
    ``out_of_time`` says the deadline has passed, the search stops between
    satellites and reports how far it got, so the caller can publish a list
    that says it is incomplete instead of a late one or none.
    """
    begin = _as_utc(start)
    t0 = ts.from_datetime(begin)
    t1 = ts.from_datetime(begin + timedelta(hours=hours))

    passes: list[Pass] = []
    searched = 0
    for item in tracked:
        if out_of_time is not None and out_of_time():
            log.warning(
                "pass search stopped at the run deadline after %d of %d satellites",
                searched,
                len(tracked),
            )
            break
        searched += 1
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
    return PassSearch(passes=passes, searched=searched, total=len(tracked))


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
    """Upcoming passes over the site, soonest first, searched to completion."""
    return search_passes(
        tracked,
        site,
        ts,
        start,
        hours=hours,
        min_elevation_deg=min_elevation_deg,
        max_per_satellite=max_per_satellite,
    ).passes


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
                # A single pass can culminate more than once when the elevation
                # curve wobbles. Keep the highest, not the last, so
                # peak_elevation_deg is the actual maximum of the pass.
                previous = pending.get(_CULMINATE)
                if previous is None or _elevation_at(item, site, t) > _elevation_at(
                    item, site, previous
                ):
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


def _elevation_at(item: TrackedSatellite, site: GeographicPosition, t) -> float:
    """Elevation in degrees, or -inf if it cannot be computed.

    Returning -inf rather than raising keeps the caller's comparison total, so
    a bad sample loses the max rather than aborting the pass search.
    """
    try:
        return float((item.satellite - site).at(t).altaz()[0].degrees)
    except Exception:
        return float("-inf")


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
