"""Build entry point: fetch everything, compute everything, write static JSON.

Run with ``uv run python -m orbital_watch.build``.

The contract this module upholds is the one that makes the whole project work:
**it completes.** Any single upstream source can be down, slow, or returning
nonsense, and this still produces a valid site with an honest account of what
is missing. No upstream failure can make it exit non-zero; it returns 1 only
if it cannot write output at all, and 2 if our own code has a bug.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orbital_watch import health
from orbital_watch.config import (
    BUILD_DIR,
    CACHE_DIR,
    CELESTRAK_MIN_REFETCH_INTERVAL,
    DATA_SUBDIR,
    DEFAULT_OBSERVER,
    HORIZON_ELEVATION_DEG,
    KP_ELEVATED_THRESHOLD,
    KP_STORM_THRESHOLD,
    MAX_PASSES_TOTAL,
    MIN_PASS_ELEVATION_DEG,
    PASS_SEARCH_HOURS,
    SPACE_WEATHER_STALENESS,
    TLE_STALENESS,
    TRACKED_GROUPS,
    WEB_DIR,
    Observer,
)
from orbital_watch.propagate import (
    Pass,
    SkyPosition,
    build_satellites,
    next_passes,
    site_from,
    sky_view,
    timescale,
)
from orbital_watch.sources import celestrak, swpc
from orbital_watch.sources.fetch import format_duration

log = logging.getLogger("orbital_watch.build")

SCHEMA_VERSION = 1


def configure_logging(verbose: bool = False) -> None:
    """Structured, levelled logging. No print() anywhere in library code."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )


# --------------------------------------------------------------------------
# Stage 1: satellites
# --------------------------------------------------------------------------


def collect_satellites(
    cache_dir: Path, ts, *, now: datetime | None = None
) -> tuple[list, list, list]:
    """Fetch every tracked group and build propagatable satellites.

    Returns (tracked_satellites, source_health, group_coverage). A group that
    fails contributes zero satellites and one FAILED health entry -- it does
    not stop the others.

    ``group_coverage`` records how many objects each group offered versus how
    many we actually propagate, so the page can state its own sampling rather
    than implying it tracks everything.
    """
    tracked: list = []
    reports: list[health.SourceHealth] = []
    coverage: list[dict] = []

    for group in TRACKED_GROUPS:
        result = celestrak.fetch_group(group, cache_dir)
        reports.append(
            health.evaluate(
                result, TLE_STALENESS, label=f"Celestrak — {group.label}", now=now
            )
        )

        if not result.ok:
            log.warning(
                "group=%s unavailable, continuing without it (%s)",
                group.key,
                result.error,
            )
            coverage.append(
                {
                    "key": group.key,
                    "label": group.label,
                    "available": 0,
                    "tracked": 0,
                    "sampled": False,
                }
            )
            continue

        available = len(result.data)
        selected = celestrak.select_objects(result.data, group)
        built = build_satellites(selected, group, ts)
        tracked.extend(built)
        coverage.append(
            {
                "key": group.key,
                "label": group.label,
                "available": available,
                "tracked": len(built),
                "sampled": len(built) < available,
            }
        )

    log.info(
        "tracking %d satellites across %d group(s)", len(tracked), len(TRACKED_GROUPS)
    )
    return tracked, reports, coverage


# --------------------------------------------------------------------------
# Stage 2: space weather
# --------------------------------------------------------------------------


def empty_weather() -> dict[str, Any]:
    """The space-weather block with every field absent.

    A single definition of "we know nothing" means the frontend always receives
    the same keys whether the fetch succeeded, partly succeeded, or the whole
    stage fell over. Panels can then check one flag instead of guarding every
    field individually.
    """
    return {
        "kp": None,
        "kp_label": None,
        "kp_description": None,
        "kp_time": None,
        "kp_trend": "unknown",
        "kp_history": [],
        "elevated": False,
        "storm": False,
        "solar_wind_speed_km_s": None,
        "solar_wind_time": None,
        "bt_nt": None,
        "bz_nt": None,
        "mag_time": None,
        "f107_flux": None,
        "sunspot_number": None,
        "solar_cycle_month": None,
        "available": False,
    }


def collect_space_weather(
    cache_dir: Path, *, now: datetime | None = None
) -> tuple[dict, list]:
    """Fetch and interpret space weather. Degrades field by field."""
    reports: list[health.SourceHealth] = []

    kp_result = swpc.fetch_kp(cache_dir)
    reports.append(
        health.evaluate(
            kp_result,
            SPACE_WEATHER_STALENESS,
            label="NOAA SWPC — Planetary K-index",
            now=now,
        )
    )

    wind_result = swpc.fetch_solar_wind(cache_dir)
    reports.append(
        health.evaluate(
            wind_result,
            SPACE_WEATHER_STALENESS,
            label="NOAA SWPC — Solar Wind Speed",
            now=now,
        )
    )

    mag_result = swpc.fetch_mag_field(cache_dir)
    reports.append(
        health.evaluate(
            mag_result,
            SPACE_WEATHER_STALENESS,
            label="NOAA SWPC — IMF (Bt/Bz)",
            now=now,
        )
    )

    cycle_result = swpc.fetch_solar_cycle(cache_dir)
    reports.append(
        health.evaluate(
            cycle_result,
            SPACE_WEATHER_STALENESS,
            label="NOAA SWPC — Solar Cycle",
            now=now,
        )
    )

    weather = empty_weather()

    if kp_result.ok:
        readings = swpc.parse_kp_series(kp_result.data)
        if readings:
            latest = readings[-1]
            label, description = swpc.classify_kp(latest.kp)
            weather.update(
                kp=round(latest.kp, 2),
                kp_label=label,
                kp_description=description,
                kp_time=latest.time_tag.isoformat(),
                kp_trend=swpc.kp_trend(readings),
                kp_history=[
                    {"time": r.time_tag.isoformat(), "kp": round(r.kp, 2)}
                    for r in readings[-56:]
                ],
                elevated=swpc.is_elevated(latest.kp),
                storm=swpc.is_storm(latest.kp),
                available=True,
            )
        else:
            log.warning("kp payload fetched but no readings could be parsed")

    if wind_result.ok:
        speed, stamp = swpc.latest_value(wind_result.data, "proton_speed", "speed")
        weather["solar_wind_speed_km_s"] = round(speed, 1) if speed is not None else None
        weather["solar_wind_time"] = stamp.isoformat() if stamp else None

    if mag_result.ok:
        bt, stamp = swpc.latest_value(mag_result.data, "bt")
        bz, _ = swpc.latest_value(mag_result.data, "bz_gsm", "bz")
        weather["bt_nt"] = round(bt, 1) if bt is not None else None
        weather["bz_nt"] = round(bz, 1) if bz is not None else None
        weather["mag_time"] = stamp.isoformat() if stamp else None

    if cycle_result.ok and cycle_result.data:
        row = cycle_result.data[-1]
        if isinstance(row, dict):
            f107 = row.get("f10.7")
            ssn = row.get("ssn")
            weather["f107_flux"] = (
                f107 if isinstance(f107, int | float) and f107 > 0 else None
            )
            weather["sunspot_number"] = (
                ssn if isinstance(ssn, int | float) and ssn > 0 else None
            )
            weather["solar_cycle_month"] = row.get("time-tag")

    return weather, reports


# --------------------------------------------------------------------------
# Stage 3: the operational correlation
# --------------------------------------------------------------------------

#: Why this dashboard correlates two apparently unrelated feeds. Shown on the
#: page verbatim so the reasoning is visible, not just the conclusion.
CORRELATION_EXPLANATION = (
    "Geomagnetic activity and orbital mechanics are not separate concerns for a "
    "ground-segment team. Energy deposited into the upper atmosphere during a "
    "geomagnetic storm heats and expands the thermosphere, which raises neutral "
    "density at low-Earth-orbit altitudes. Higher density means more drag: "
    "satellites lose altitude faster, along-track position error grows between "
    "element-set updates, and published TLEs go out of date sooner than usual. "
    "The same disturbance degrades the links used to talk to those satellites - "
    "HF propagation becomes unreliable at high latitudes, and ionospheric "
    "scintillation can disrupt UHF and L-band signals, including GPS."
)


def build_correlation(weather: dict) -> dict:
    """Decide whether to raise the operational banner, and say why."""
    if not weather.get("available"):
        return {
            "active": False,
            "severity": "unknown",
            "headline": "Space weather unavailable",
            "detail": (
                "Kp index could not be retrieved this run, so no geomagnetic "
                "assessment can be made. Pass predictions below are unaffected, "
                "but treat drag-driven error growth as unquantified."
            ),
            "explanation": CORRELATION_EXPLANATION,
            "operational_impacts": [],
        }

    kp = weather["kp"]

    if weather["storm"]:
        return {
            "active": True,
            "severity": "storm",
            "headline": f"Geomagnetic storm in progress — Kp {kp:g}",
            "detail": (
                f"Kp is {kp:g}, at or above the storm threshold of "
                f"{KP_STORM_THRESHOLD:g}. A ground-segment team would expect "
                f"measurably increased atmospheric drag on LEO assets, faster "
                f"decay of TLE accuracy, and degraded HF and satellite links."
            ),
            "explanation": CORRELATION_EXPLANATION,
            "operational_impacts": [
                "Increased along-track tracking error; consider tightening the "
                "element-set refresh cycle.",
                "Elevated drag on LEO assets; station-keeping budgets may be "
                "consumed faster than nominal.",
                "HF links unreliable at high latitude; expect polar-route "
                "communication outages.",
                "Ionospheric scintillation may degrade UHF/L-band, including GNSS "
                "timing and positioning.",
            ],
        }

    if weather["elevated"]:
        return {
            "active": True,
            "severity": "elevated",
            "headline": f"Elevated geomagnetic activity — Kp {kp:g}",
            "detail": (
                f"Kp is {kp:g}, at or above the elevated threshold of "
                f"{KP_ELEVATED_THRESHOLD:g} but below the storm threshold of "
                f"{KP_STORM_THRESHOLD:g}. Operators would begin watching for "
                f"drag-induced tracking error and marginal link degradation."
            ),
            "explanation": CORRELATION_EXPLANATION,
            "operational_impacts": [
                "Minor growth in along-track error between element-set updates.",
                "Occasional HF degradation at high latitude.",
            ],
        }

    return {
        "active": False,
        "severity": "quiet",
        "headline": f"Geomagnetic conditions nominal — Kp {kp:g}",
        "detail": (
            f"Kp is {kp:g}, below the elevated threshold of "
            f"{KP_ELEVATED_THRESHOLD:g}. Drag and link conditions are nominal; "
            f"standard element-set refresh cadence is sufficient."
        ),
        "explanation": CORRELATION_EXPLANATION,
        "operational_impacts": [],
    }


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------


def _sky_to_dict(position: SkyPosition, now: datetime) -> dict:
    return {
        "name": position.name,
        "norad_id": position.norad_id,
        "group": position.group,
        "elevation_deg": round(position.elevation_deg, 2),
        "azimuth_deg": round(position.azimuth_deg, 2),
        "compass": position.compass,
        "range_km": round(position.range_km, 1),
        "sub_lat_deg": round(position.sub_lat_deg, 3),
        "sub_lon_deg": round(position.sub_lon_deg, 3),
        "altitude_km": round(position.altitude_km, 1),
        "tle_epoch": position.tle_epoch.isoformat(),
        # Uses the build clock, not wall-clock: the reported element age has
        # to describe the same instant the position was computed for.
        "tle_age_hours": round((now - position.tle_epoch).total_seconds() / 3600.0, 1),
    }


def _pass_to_dict(item: Pass) -> dict:
    return {
        "name": item.name,
        "norad_id": item.norad_id,
        "group": item.group,
        "start": item.start.isoformat(),
        "peak": item.peak.isoformat(),
        "end": item.end.isoformat(),
        "peak_elevation_deg": round(item.peak_elevation_deg, 1),
        "duration_seconds": round(item.duration_seconds),
        "duration_minutes": round(item.duration_minutes, 1),
        "start_compass": item.start_compass,
        "peak_compass": item.peak_compass,
        "end_compass": item.end_compass,
    }


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False))
    log.info("wrote %s (%d bytes)", path.name, path.stat().st_size)


def copy_frontend(web_dir: Path, build_dir: Path) -> None:
    """Copy the static frontend into the build output."""
    if not web_dir.exists():
        log.warning("web dir missing at %s; skipping frontend copy", web_dir)
        return
    build_dir.mkdir(parents=True, exist_ok=True)
    for item in web_dir.iterdir():
        target = build_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)
    log.info("copied frontend from %s to %s", web_dir, build_dir)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def run_build(
    *,
    cache_dir: Path = CACHE_DIR,
    build_dir: Path = BUILD_DIR,
    web_dir: Path = WEB_DIR,
    observer: Observer = DEFAULT_OBSERVER,
    now: datetime | None = None,
) -> dict:
    """Execute the full pipeline and write the site. Returns the manifest."""
    started = datetime.now(UTC)
    now = now or started
    log.info(
        "build start observer=%s lat=%.4f lon=%.4f",
        observer.name,
        observer.latitude_deg,
        observer.longitude_deg,
    )

    ts = timescale()
    site = site_from(observer)

    # Each stage is individually contained. fetch_json never raises, but the
    # code that interprets what it returns still can -- a cached payload whose
    # shape predates the current parser, an upstream that changed structure.
    # Since the whole promise of this build is that it completes, no single
    # stage is allowed to be the thing that stops it.
    # `now` is threaded into health classification as well as propagation so a
    # single instant governs the whole page. Letting health call its own
    # `datetime.now()` meant reported ages were measured against a different
    # moment than the pass predictions -- invisible on a fast build, wrong on a
    # slow one, and impossible to pin down in a test.
    try:
        tracked, tle_reports, coverage = collect_satellites(cache_dir, ts, now=now)
    except Exception:
        log.exception("satellite collection failed; continuing with no satellites")
        tracked, tle_reports, coverage = [], [], []

    try:
        weather, weather_reports = collect_space_weather(cache_dir, now=now)
    except Exception:
        log.exception("space weather collection failed; continuing without it")
        weather, weather_reports = empty_weather(), []

    # Propagation is pure computation on data we already hold, but a
    # pathological element set could still throw. The site is worth more than
    # any one panel, so a propagation failure degrades to empty lists.
    try:
        overhead = sky_view(
            tracked, site, ts, now, min_elevation_deg=HORIZON_ELEVATION_DEG
        )
    except Exception:
        log.exception("sky view computation failed; continuing with empty sky")
        overhead = []

    try:
        upcoming = next_passes(tracked, site, ts, now)[:MAX_PASSES_TOTAL]
    except Exception:
        log.exception("pass computation failed; continuing with no passes")
        upcoming = []

    all_reports = tle_reports + weather_reports
    health_block = health.summarize(all_reports)
    correlation = build_correlation(weather)

    elapsed = (datetime.now(UTC) - started).total_seconds()

    observer_block = {
        "name": observer.name,
        "latitude_deg": observer.latitude_deg,
        "longitude_deg": observer.longitude_deg,
        "elevation_m": observer.elevation_m,
    }
    meta = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": started.isoformat(),
        "build_seconds": round(elapsed, 2),
        "observer": observer_block,
        "parameters": {
            "horizon_elevation_deg": HORIZON_ELEVATION_DEG,
            "min_pass_elevation_deg": MIN_PASS_ELEVATION_DEG,
            "pass_search_hours": PASS_SEARCH_HOURS,
            "celestrak_min_refetch": format_duration(CELESTRAK_MIN_REFETCH_INTERVAL),
            "kp_elevated_threshold": KP_ELEVATED_THRESHOLD,
            "kp_storm_threshold": KP_STORM_THRESHOLD,
        },
        "coverage": coverage,
        "counts": {
            "tracked_satellites": len(tracked),
            "above_horizon": len(overhead),
            "upcoming_passes": len(upcoming),
        },
    }

    # Frontend first, generated data second. copy_frontend uses
    # dirs_exist_ok=True, so doing it the other way round would let a stray
    # web/data/ directory silently overwrite the JSON we just computed.
    copy_frontend(web_dir, build_dir)

    data_dir = build_dir / DATA_SUBDIR
    write_json(data_dir / "meta.json", meta)
    write_json(
        data_dir / "sky.json",
        {
            "generated_at": started.isoformat(),
            "observer": observer_block,
            "horizon_elevation_deg": HORIZON_ELEVATION_DEG,
            "satellites": [_sky_to_dict(p, now) for p in overhead],
        },
    )
    write_json(
        data_dir / "passes.json",
        {
            "generated_at": started.isoformat(),
            "observer": observer_block,
            "min_elevation_deg": MIN_PASS_ELEVATION_DEG,
            "search_hours": PASS_SEARCH_HOURS,
            "passes": [_pass_to_dict(p) for p in upcoming],
        },
    )
    write_json(
        data_dir / "space_weather.json",
        {
            "generated_at": started.isoformat(),
            "weather": weather,
            "correlation": correlation,
        },
    )
    write_json(
        data_dir / "health.json",
        {"generated_at": started.isoformat(), **health_block},
    )

    log.info(
        "build complete elapsed=%.2fs tracked=%d above_horizon=%d passes=%d health=%s",
        elapsed,
        len(tracked),
        len(overhead),
        len(upcoming),
        health_block["overall"],
    )
    degraded = [s for s in all_reports if s.state != health.State.FRESH]
    if degraded:
        log.warning(
            "%d/%d source(s) degraded: %s",
            len(degraded),
            len(all_reports),
            ", ".join(f"{s.name}={s.state.value}" for s in degraded),
        )

    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="orbital_watch.build",
        description="Fetch upstream data and write the static Orbital Watch site.",
    )
    parser.add_argument("--build-dir", type=Path, default=BUILD_DIR)
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    configure_logging(args.verbose)

    try:
        run_build(cache_dir=args.cache_dir, build_dir=args.build_dir)
    except OSError:
        # Cannot write output. Nothing useful can be produced, so fail loudly.
        log.exception("build failed: could not write output")
        return 1
    except Exception:
        # Anything else reaching here is a bug in our own code rather than an
        # upstream problem -- every upstream failure mode is handled deeper in.
        # Report it as a real failure so CI goes red instead of quietly
        # deploying a stale site, but log it fully rather than dumping a bare
        # traceback at whoever is reading the workflow output.
        log.exception("build failed: unexpected internal error")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
