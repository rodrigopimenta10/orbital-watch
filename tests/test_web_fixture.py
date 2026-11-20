"""Keeps the browser's interpolation honest against SGP4.

The page shows where satellites are by interpolating along a track this
pipeline publishes. The JavaScript that does it is tested under Node
(``tests/web/``), but a JavaScript test on its own can only check the
arithmetic against numbers somebody typed in. So the numbers come from here:
this module writes a small file holding one real track and, for instants
*between* its samples, the position SGP4 gives directly. The Node test reads
that file and requires the page's interpolation to land on SGP4's answer.

The file is committed. The test below fails if it no longer matches what the
pipeline produces, with the command to regenerate it.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from orbital_watch.config import (
    EPHEMERIS_STEP,
    EPHEMERIS_WINDOW,
    Observer,
    SatelliteGroup,
)
from orbital_watch.propagate import (
    build_satellites,
    ephemeris,
    next_passes,
    site_from,
    timescale,
)
from tests.helpers import FIXTURE_TIME

CASE_PATH = Path(__file__).parent / "web" / "fixtures" / "track_case.json"
REGENERATE_ENV = "ORBITAL_WATCH_REGENERATE_FIXTURES"


def build_case(stations_records: list[dict]) -> dict:
    """One high pass of one object: its published track, and SGP4's own answers."""
    ts = timescale()
    site = site_from(Observer("Test", 39.0840, -77.1528, 82))
    group = SatelliteGroup(key="stations", label="Stations", max_objects=50)
    tracked = build_satellites(stations_records, group, ts)

    # A pass that goes high overhead is where interpolating would be worst.
    best = max(
        next_passes(tracked, site, ts, FIXTURE_TIME, hours=24, min_elevation_deg=40.0),
        key=lambda item: item.peak_elevation_deg,
    )
    start = best.start - timedelta(minutes=5)
    tracks = ephemeris(tracked, site, ts, start)
    track = next(t for t in tracks if t.norad_id == best.norad_id)
    satellite = next(t for t in tracked if t.norad_id == best.norad_id).satellite

    truth = []
    samples = int(EPHEMERIS_WINDOW / EPHEMERIS_STEP) + 1
    for index in range(samples - 1):
        when = start + EPHEMERIS_STEP * index + EPHEMERIS_STEP / 2
        alt, az, distance = (satellite - site).at(ts.from_datetime(when)).altaz()
        if alt.degrees < 0:
            continue
        truth.append(
            {
                "at": when.isoformat(),
                "elevation_deg": round(float(alt.degrees), 4),
                "azimuth_deg": round(float(az.degrees), 4),
                "range_km": round(float(distance.km), 3),
            }
        )

    return {
        "about": (
            "Written by tests/test_web_fixture.py. One real pass: the track the "
            "pipeline would publish, and SGP4 evaluated directly at instants "
            "halfway between its samples."
        ),
        "peak_elevation_deg": round(best.peak_elevation_deg, 1),
        "sky": {
            "horizon_elevation_deg": 0.0,
            "ephemeris": {
                "start": start.isoformat(),
                "step_seconds": EPHEMERIS_STEP.total_seconds(),
                "samples": samples,
            },
            "satellites": [
                {
                    "name": track.name,
                    "norad_id": track.norad_id,
                    "group": track.group,
                    "tle_epoch": track.tle_epoch.isoformat(),
                    "track": [list(sample) for sample in track.samples],
                }
            ],
        },
        "sgp4": truth,
    }


def test_committed_browser_fixture_matches_the_pipeline(stations_records):
    case = build_case(stations_records)
    assert len(case["sgp4"]) >= 10, "the pass should be above the horizon for minutes"
    assert case["peak_elevation_deg"] >= 40

    if os.environ.get(REGENERATE_ENV):
        CASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CASE_PATH.write_text(json.dumps(case, indent=1) + "\n")

    assert CASE_PATH.exists(), f"missing; run {REGENERATE_ENV}=1 uv run pytest {__file__}"
    committed = json.loads(CASE_PATH.read_text())

    # Compared with a tolerance far below anything the page displays, so that
    # a last-digit difference between platforms' maths libraries does not
    # fail the build while a real change to the track still does.
    assert committed["sky"]["ephemeris"] == case["sky"]["ephemeris"]
    ours, theirs = case["sky"]["satellites"][0], committed["sky"]["satellites"][0]
    assert theirs["name"] == ours["name"]
    for got, want in zip(theirs["track"], ours["track"], strict=True):
        assert got == pytest.approx(want, abs=0.15), (
            f"track drifted; run {REGENERATE_ENV}=1 uv run pytest {__file__}"
        )
    for got, want in zip(committed["sgp4"], case["sgp4"], strict=True):
        assert got["at"] == want["at"]
        assert got["elevation_deg"] == pytest.approx(want["elevation_deg"], abs=1e-3)
        assert got["range_km"] == pytest.approx(want["range_km"], abs=1e-2)
