"""Propagation correctness.

The headline test here checks our propagation chain against the published
SGP4 verification vectors from Vallado et al., "Revisiting Spacetrack Report
#3" (AIAA 2006-6753) -- an independently-known result, not a value this
project generated and then froze. If the numbers below ever change, either a
dependency has broken SGP4 or we have wired it up wrong.

Everything else runs off committed fixtures captured from the real Celestrak
API, propagated to the time they were captured rather than to today: element
sets months past their epoch describe orbits that have since changed, and a
test that depends on how old a checked-in file is fails for reasons that have
nothing to do with the code. No test in this file touches the network.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest
from sgp4.api import WGS72, Satrec

from orbital_watch.config import Observer, SatelliteGroup
from orbital_watch.propagate import (
    build_satellites,
    compass_from_azimuth,
    ephemeris,
    next_passes,
    position_from_track,
    search_passes,
    site_from,
    sky_view,
    timescale,
)
from tests.helpers import FIXTURE_TIME

# --------------------------------------------------------------------------
# Reference case: SGP4 verification satellite 00005
# --------------------------------------------------------------------------

VALLADO_TLE_LINE1 = (
    "1 00005U 58002B   00179.78495062  .00000023  00000-0  28098-4 0  4753"
)
VALLADO_TLE_LINE2 = (
    "2 00005  34.2682 348.7242 1859667 331.7664  19.3264 10.82419157413667"
)

#: Expected TEME position/velocity at epoch and epoch+360 min, straight from
#: the reference implementation's published output (tcppver.out).
VALLADO_EXPECTED = {
    0.0: (
        (7022.46529266, -1400.08296755, 0.03995155),
        (1.893841015, 6.405893759, 4.534807250),
    ),
    360.0: (
        (-7154.03120202, -3783.17682504, -3536.19412294),
        (4.741887409, -4.151817765, -2.093935425),
    ),
}


@pytest.mark.parametrize("tsince", sorted(VALLADO_EXPECTED))
def test_sgp4_matches_published_verification_vectors(tsince):
    """Our SGP4 reproduces the reference implementation to sub-metre precision."""
    satellite = Satrec.twoline2rv(VALLADO_TLE_LINE1, VALLADO_TLE_LINE2, WGS72)
    error, position, velocity = satellite.sgp4_tsince(tsince)

    assert error == 0, f"SGP4 reported error code {error}"

    expected_r, expected_v = VALLADO_EXPECTED[tsince]
    for axis, (got, want) in enumerate(zip(position, expected_r, strict=True)):
        assert got == pytest.approx(want, abs=1e-6), f"position axis {axis}"
    for axis, (got, want) in enumerate(zip(velocity, expected_v, strict=True)):
        assert got == pytest.approx(want, abs=1e-8), f"velocity axis {axis}"


# --------------------------------------------------------------------------
# Physical sanity against real element sets
# --------------------------------------------------------------------------

STATIONS = SatelliteGroup(key="stations", label="Stations", max_objects=50)
WEATHER = SatelliteGroup(key="weather", label="Weather", max_objects=80)


def test_iss_altitude_and_period_are_physical(stations_records):
    """The ISS should sit in LEO at a plausible altitude with a ~93 min period."""
    ts = timescale()
    tracked = build_satellites(stations_records, STATIONS, ts)

    iss = next((s for s in tracked if "ZARYA" in s.name.upper()), None)
    assert iss is not None, "ISS (ZARYA) missing from the stations fixture"

    t = ts.from_datetime(iss.epoch)
    subpoint = iss.satellite.at(t).subpoint()
    altitude_km = subpoint.elevation.km

    # The ISS is maintained roughly between 370 and 460 km.
    assert 350 < altitude_km < 480, f"implausible ISS altitude {altitude_km:.1f} km"

    # no_kozai is the mean motion in radians per minute, so the orbital
    # period is 2*pi/n. The ISS sits close to 93 minutes.
    period_minutes = 2 * math.pi / iss.satellite.model.no_kozai
    assert 88 < period_minutes < 96, f"implausible ISS period {period_minutes:.2f} min"


def test_geostationary_satellites_sit_at_geostationary_altitude(weather_records):
    """GOES spacecraft are geostationary; that altitude is a known constant.

    Geostationary radius is ~42,164 km, i.e. ~35,786 km above the surface.
    This is an independent physical check on the whole GP-record-to-position
    chain: nothing in our code knows that number.
    """
    ts = timescale()
    tracked = build_satellites(weather_records, WEATHER, ts)

    goes = [s for s in tracked if s.name.upper().startswith("GOES")]
    assert goes, "no GOES spacecraft in the weather fixture"

    t = ts.from_datetime(FIXTURE_TIME)
    checked = 0
    for satellite in goes:
        altitude_km = satellite.satellite.at(t).subpoint().elevation.km
        # Older GOES are in graveyard/inclined orbits; only assert on the ones
        # actually near GEO, but require that we found some.
        if 30000 < altitude_km < 40000:
            assert altitude_km == pytest.approx(35786, abs=400), satellite.name
            checked += 1
    assert checked >= 1, "expected at least one GOES near geostationary altitude"


def test_sky_view_only_returns_satellites_above_the_horizon(stations_records):
    ts = timescale()
    site = site_from(Observer("Test", 39.0840, -77.1528, 82))
    tracked = build_satellites(stations_records, STATIONS, ts)

    visible = sky_view(tracked, site, ts, FIXTURE_TIME, min_elevation_deg=0.0)

    assert all(p.elevation_deg >= 0.0 for p in visible)
    # Sorted highest first, so an operator reads the best target at the top.
    assert visible == sorted(visible, key=lambda p: p.elevation_deg, reverse=True)


def test_passes_are_wellformed_and_ordered(stations_records):
    """Every emitted pass must be internally consistent."""
    ts = timescale()
    site = site_from(Observer("Test", 39.0840, -77.1528, 82))
    tracked = build_satellites(stations_records, STATIONS, ts)
    start = FIXTURE_TIME

    passes = next_passes(tracked, site, ts, start, hours=24, min_elevation_deg=10.0)

    assert passes, "expected at least one pass over 24 hours from the stations group"
    for item in passes:
        assert item.start < item.peak < item.end, f"{item.name} has disordered events"
        assert item.duration_seconds > 0
        # A pass above 10 degrees from LEO lasts minutes, not hours.
        assert item.duration_seconds < 3 * 3600
        assert item.peak_elevation_deg >= 10.0 - 0.5
        assert start - timedelta(minutes=1) <= item.start
    assert passes == sorted(passes, key=lambda p: p.start), "passes not sorted by start"


def test_pass_search_respects_minimum_elevation(stations_records):
    """Raising the elevation mask cannot produce more passes."""
    ts = timescale()
    site = site_from(Observer("Test", 39.0840, -77.1528, 82))
    tracked = build_satellites(stations_records, STATIONS, ts)
    start = FIXTURE_TIME

    low = next_passes(tracked, site, ts, start, hours=24, min_elevation_deg=10.0)
    high = next_passes(tracked, site, ts, start, hours=24, min_elevation_deg=40.0)

    assert len(high) <= len(low)
    assert all(p.peak_elevation_deg >= 39.5 for p in high)


# --------------------------------------------------------------------------
# Degradation inside propagation
# --------------------------------------------------------------------------


def test_malformed_records_are_skipped_not_fatal(stations_records):
    """One unusable element set costs one satellite, not the whole group."""
    ts = timescale()
    corrupted = [
        {"OBJECT_NAME": "BROKEN", "NORAD_CAT_ID": 99999},  # missing everything
        *stations_records,
        {"OBJECT_NAME": "ALSO BROKEN", "NORAD_CAT_ID": "not-an-int", "EPOCH": "nope"},
    ]

    tracked = build_satellites(corrupted, STATIONS, ts)

    assert len(tracked) == len(stations_records)
    assert not any(s.name in ("BROKEN", "ALSO BROKEN") for s in tracked)


def test_empty_input_yields_empty_output():
    ts = timescale()
    site = site_from(Observer("Test", 39.0840, -77.1528, 82))

    assert build_satellites([], STATIONS, ts) == []
    assert sky_view([], site, ts, FIXTURE_TIME) == []
    assert next_passes([], site, ts, FIXTURE_TIME) == []


@pytest.mark.parametrize(
    ("azimuth", "expected"),
    [
        (0.0, "N"),
        (11.24, "N"),
        (11.26, "NNE"),
        (90.0, "E"),
        (180.0, "S"),
        (270.0, "W"),
        (359.9, "N"),
        (360.0, "N"),
        (450.0, "E"),  # wraps
    ],
)
def test_compass_conversion(azimuth, expected):
    assert compass_from_azimuth(azimuth) == expected


# --------------------------------------------------------------------------
# The position track the browser interpolates along
# --------------------------------------------------------------------------

SITE = Observer("Test", 39.0840, -77.1528, 82)
WINDOW = timedelta(minutes=30)
STEP = timedelta(seconds=30)


def _tracked_stations(stations_records):
    ts = timescale()
    return ts, site_from(SITE), build_satellites(stations_records, STATIONS, ts)


def _a_pass_start(stations_records) -> datetime:
    """A moment shortly before a high pass, so a track spans rise to set."""
    ts, site, tracked = _tracked_stations(stations_records)
    passes = next_passes(
        tracked, site, ts, FIXTURE_TIME, hours=24, min_elevation_deg=40.0
    )
    assert passes, "the stations fixture should yield a high pass within a day"
    return passes[0].start - timedelta(minutes=5)


def test_track_has_one_sample_per_step_across_the_window(stations_records):
    ts, site, tracked = _tracked_stations(stations_records)

    tracks = ephemeris(
        tracked, site, ts, _a_pass_start(stations_records), window=WINDOW, step=STEP
    )

    assert tracks
    for track in tracks:
        assert len(track.samples) == 61
        assert all(len(sample) == 4 for sample in track.samples)
        assert all(math.isfinite(v) for sample in track.samples for v in sample)


def test_only_objects_that_come_near_the_sky_get_a_track(stations_records):
    """The document carries tracks for what a reader could see, not everything."""
    ts, site, tracked = _tracked_stations(stations_records)
    start = _a_pass_start(stations_records)

    tracks = ephemeris(tracked, site, ts, start, window=WINDOW, step=STEP)

    assert 0 < len(tracks) < len(tracked), "some objects are below the horizon"
    for track in tracks:
        peak = max(
            position_from_track(track.samples, start, STEP, start + STEP * i)[0]
            for i in range(len(track.samples))
        )
        assert peak >= -2.0, f"{track.name} never comes within 2 deg of the horizon"

    # Raising the bar to "well up" keeps only objects that actually get there.
    high = ephemeris(
        tracked, site, ts, start, window=WINDOW, step=STEP, min_elevation_deg=40.0
    )
    assert 0 < len(high) <= len(tracks)


def test_interpolated_track_matches_sgp4_between_samples(stations_records):
    """The accuracy claim the sky view rests on, checked against SGP4 itself.

    The browser never propagates anything: it draws a straight line between
    two published samples. This compares that straight line, at the instants
    furthest from any sample, with a direct SGP4 evaluation -- across a pass
    that goes high overhead, where angles change fastest and interpolating
    them directly would be worst.
    """
    ts, site, tracked = _tracked_stations(stations_records)
    start = _a_pass_start(stations_records)
    by_name = {item.name: item for item in tracked}

    tracks = ephemeris(tracked, site, ts, start, window=WINDOW, step=STEP)
    worst_angle = 0.0
    worst_range = 0.0
    checked = 0
    for track in tracks:
        satellite = by_name[track.name].satellite
        for index in range(len(track.samples) - 1):
            when = start + STEP * index + STEP / 2
            elevation, azimuth, rng = position_from_track(
                track.samples, start, STEP, when
            )
            if elevation < 0:
                continue
            alt, az, distance = (satellite - site).at(ts.from_datetime(when)).altaz()
            # Angular separation on the sky between the interpolated direction
            # and the true one. Comparing azimuths directly would overstate
            # the error near the zenith, where a degree of azimuth is nothing.
            separation = math.degrees(
                math.acos(
                    min(
                        1.0,
                        math.sin(math.radians(elevation)) * math.sin(alt.radians)
                        + math.cos(math.radians(elevation))
                        * math.cos(alt.radians)
                        * math.cos(math.radians(azimuth) - az.radians),
                    )
                )
            )
            worst_angle = max(worst_angle, separation)
            worst_range = max(worst_range, abs(rng - distance.km))
            checked += 1

    assert checked > 10, "expected the track to cover objects above the horizon"
    assert worst_angle < 0.2, f"interpolation is off by {worst_angle:.3f} deg"
    assert worst_range < 2.0, f"interpolated range is off by {worst_range:.2f} km"


def test_track_samples_reproduce_the_position_at_each_sample(stations_records):
    """At a sample instant the track is exact to its rounding."""
    ts, site, tracked = _tracked_stations(stations_records)
    start = _a_pass_start(stations_records)
    by_name = {item.name: item for item in tracked}

    track = ephemeris(tracked, site, ts, start, window=WINDOW, step=STEP)[0]
    when = start + STEP * 10
    elevation, azimuth, rng = position_from_track(track.samples, start, STEP, when)
    alt, az, distance = (
        (by_name[track.name].satellite - site).at(ts.from_datetime(when)).altaz()
    )

    assert rng == pytest.approx(distance.km, abs=0.2)
    assert elevation == pytest.approx(alt.degrees, abs=0.05)
    if alt.degrees < 85:
        assert azimuth == pytest.approx(az.degrees, abs=0.1)


def test_geostationary_track_barely_moves(weather_records):
    """A second physical check: from the ground, GEO spacecraft stand still."""
    ts = timescale()
    site = site_from(SITE)
    tracked = build_satellites(weather_records, WEATHER, ts)

    tracks = ephemeris(tracked, site, ts, FIXTURE_TIME, window=WINDOW, step=STEP)
    geostationary = [t for t in tracks if 35000 < t.samples[0][3] < 36500]

    assert geostationary, "expected a geostationary object above this site's horizon"
    steadiest = min(
        geostationary,
        key=lambda t: math.dist(t.samples[0][:3], t.samples[-1][:3]),
    )
    first = position_from_track(steadiest.samples, FIXTURE_TIME, STEP, FIXTURE_TIME)
    last = position_from_track(
        steadiest.samples, FIXTURE_TIME, STEP, FIXTURE_TIME + WINDOW
    )
    assert abs(first[0] - last[0]) < 0.5, f"{steadiest.name} moved in elevation"


def test_position_outside_the_track_is_none_not_an_extrapolation():
    """Past the end of the track there is no position, only a guess."""
    samples = [(0.0, 100.0, 50.0, 400.0), (10.0, 100.0, 50.0, 400.0)]

    assert position_from_track(samples, FIXTURE_TIME, STEP, FIXTURE_TIME - STEP) is None
    assert (
        position_from_track(samples, FIXTURE_TIME, STEP, FIXTURE_TIME + STEP * 2) is None
    )
    assert (
        position_from_track(samples, FIXTURE_TIME, STEP, FIXTURE_TIME + STEP) is not None
    )


def test_object_that_cannot_be_propagated_costs_one_track(stations_records):
    """SGP4 reports a hopeless element set as NaN, which must not reach JSON."""
    ts, site, tracked = _tracked_stations(stations_records)
    start = _a_pass_start(stations_records)
    healthy = ephemeris(tracked, site, ts, start, window=WINDOW, step=STEP)

    # An eccentricity of 1 or more is not an orbit; SGP4 returns NaN for it.
    broken = dict(stations_records[0], OBJECT_NAME="NOT AN ORBIT", ECCENTRICITY=1.5)
    with_broken = build_satellites([broken, *stations_records], STATIONS, ts)
    tracks = ephemeris(with_broken, site, ts, start, window=WINDOW, step=STEP)

    assert "NOT AN ORBIT" not in {t.name for t in tracks}
    assert {t.name for t in tracks} == {t.name for t in healthy}
    assert all(math.isfinite(v) for t in tracks for s in t.samples for v in s)


def test_pass_search_stops_at_the_deadline_and_says_how_far_it_got(stations_records):
    """Out of time means a shorter list that admits it, not a late run."""
    ts, site, tracked = _tracked_stations(stations_records)
    budget = iter([False, False, False])

    search = search_passes(
        tracked, site, ts, FIXTURE_TIME, out_of_time=lambda: next(budget, True)
    )

    assert search.searched == 3
    assert search.total == len(tracked)
    assert not search.complete
    assert {p.name for p in search.passes} <= {item.name for item in tracked[:3]}

    full = search_passes(tracked, site, ts, FIXTURE_TIME)
    assert full.complete
    assert full.searched == full.total == len(tracked)
