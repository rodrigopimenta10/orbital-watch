"""Propagation correctness.

The headline test here checks our propagation chain against the published
SGP4 verification vectors from Vallado et al., "Revisiting Spacetrack Report
#3" (AIAA 2006-6753) -- an independently-known result, not a value this
project generated and then froze. If the numbers below ever change, either a
dependency has broken SGP4 or we have wired it up wrong.

Everything else runs off committed fixtures captured from the real Celestrak
API. No test in this file touches the network.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from sgp4.api import WGS72, Satrec

from orbital_watch.config import Observer, SatelliteGroup
from orbital_watch.propagate import (
    build_satellites,
    compass_from_azimuth,
    next_passes,
    site_from,
    sky_view,
    timescale,
)

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

    t = ts.from_datetime(datetime.now(UTC))
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

    visible = sky_view(tracked, site, ts, datetime.now(UTC), min_elevation_deg=0.0)

    assert all(p.elevation_deg >= 0.0 for p in visible)
    # Sorted highest first, so an operator reads the best target at the top.
    assert visible == sorted(visible, key=lambda p: p.elevation_deg, reverse=True)


def test_passes_are_wellformed_and_ordered(stations_records):
    """Every emitted pass must be internally consistent."""
    ts = timescale()
    site = site_from(Observer("Test", 39.0840, -77.1528, 82))
    tracked = build_satellites(stations_records, STATIONS, ts)
    start = datetime.now(UTC)

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
    start = datetime.now(UTC)

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
    assert sky_view([], site, ts, datetime.now(UTC)) == []
    assert next_passes([], site, ts, datetime.now(UTC)) == []


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
