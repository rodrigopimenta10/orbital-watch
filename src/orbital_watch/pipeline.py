"""One refresh cycle: fetch what is due, propagate, evaluate health, assemble.

:func:`build_document` is the whole of it. The contract it upholds is the one
the project rests on: **it returns a complete document.** Any upstream source
can be down, slow, or returning nonsense, and the result is still a full,
valid document with an honest account of what is missing. Publishing that
document is a separate step (:mod:`orbital_watch.document`), because "we
could not reach NOAA" and "we could not write to our own bucket" are different
kinds of failure and only the second should fail the run.
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from orbital_watch import health
from orbital_watch.config import (
    CELESTRAK_MIN_REFETCH_INTERVAL,
    CELESTRAK_NETWORK_BUDGET_SECONDS,
    CELESTRAK_PUBLICATION_INTERVAL,
    CELESTRAK_RETRY_INTERVAL,
    COMPUTE_RESERVE_SECONDS,
    DEFAULT_OBSERVER,
    EPHEMERIS_STEP,
    EPHEMERIS_WINDOW,
    HORIZON_ELEVATION_DEG,
    KP_ELEVATED_THRESHOLD,
    KP_STORM_THRESHOLD,
    MAX_PASSES_TOTAL,
    MIN_PASS_ELEVATION_DEG,
    PASS_SEARCH_HOURS,
    PIPELINE_SILENCE_LIMIT,
    PUBLISH_RESERVE_SECONDS,
    REFRESH_CADENCE,
    REVISION_ENV,
    RUN_DEADLINE_SECONDS,
    SPACE_WEATHER_STALENESS,
    SWPC_NETWORK_BUDGET_SECONDS,
    TLE_STALENESS,
    TRACKED_GROUPS,
    Observer,
    StalenessPolicy,
)
from orbital_watch.document import DOCUMENT_KIND, SCHEMA_VERSION
from orbital_watch.propagate import (
    Pass,
    PassSearch,
    Track,
    build_satellites,
    ephemeris,
    search_passes,
    site_from,
    sky_view,
    timescale,
)
from orbital_watch.sources import celestrak, swpc
from orbital_watch.sources.fetch import (
    FetchResult,
    NetworkBudget,
    Outcome,
    SourceSpec,
    fetch_source,
    utcnow,
)
from orbital_watch.store import Store

log = logging.getLogger(__name__)

#: Every source the health panel must account for, in display order. The
#: document is checked against this list before it is published, so a run
#: cannot quietly drop a source from the panel.
SOURCE_LABELS: dict[str, str] = {
    **{
        celestrak.source_name(group): f"Celestrak — {group.label}"
        for group in TRACKED_GROUPS
    },
    swpc.KP.name: "NOAA SWPC — Planetary K-index",
    swpc.SOLAR_WIND.name: "NOAA SWPC — Solar Wind Speed",
    swpc.MAG_FIELD.name: "NOAA SWPC — IMF (Bt/Bz)",
    swpc.SOLAR_CYCLE.name: "NOAA SWPC — Solar Cycle",
}


class Deadline:
    """The run's overall wall-clock allowance."""

    def __init__(self, seconds: float = RUN_DEADLINE_SECONDS) -> None:
        self._ends = time.monotonic() + seconds

    def remaining(self) -> float:
        return self._ends - time.monotonic()

    def too_late_to_compute(self) -> bool:
        """Whether a long computation should stop so the run can still publish."""
        return self.remaining() <= PUBLISH_RESERVE_SECONDS

    def network_must_end_by(self) -> float:
        """The monotonic instant after which no upstream request may still run.

        Upstreams get what is left of the run once computing and publishing
        have been provided for, and not a second of what those need. Without
        this the run deadline was only a hope: a slow upstream could take as
        long as it liked, and the time came out of the pass search and then
        out of publication itself.
        """
        return self._ends - PUBLISH_RESERVE_SECONDS - COMPUTE_RESERVE_SECONDS


# --------------------------------------------------------------------------
# Stage 1: space weather
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
    store: Store,
    *,
    clock: Callable[[], datetime] = utcnow,
    not_after: float | None = None,
) -> tuple[dict, list[health.SourceHealth]]:
    """Fetch and interpret space weather. Degrades field by field.

    ``not_after`` is the monotonic instant by which every request must be
    over, from the run's deadline.
    """
    # One budget across the four SWPC reads: individually bounded requests
    # still serialise into minutes when everything is slow, and the budget
    # caps the sum. It is separate from Celestrak's so that one provider
    # being slow cannot spend the other's allowance.
    budget = NetworkBudget(SWPC_NETWORK_BUDGET_SECONDS, not_after=not_after)

    results: dict[str, FetchResult] = {}
    reports: list[health.SourceHealth] = []
    for spec in (swpc.KP, swpc.SOLAR_WIND, swpc.MAG_FIELD, swpc.SOLAR_CYCLE):
        result = fetch_source(
            spec, store, deadline=budget.deadline_for(spec.max_seconds), clock=clock
        )
        results[spec.name] = result
        reports.append(
            health.evaluate(
                result,
                SPACE_WEATHER_STALENESS,
                label=SOURCE_LABELS[spec.name],
                now=clock(),
            )
        )

    kp_result = results[swpc.KP.name]
    wind_result = results[swpc.SOLAR_WIND.name]
    mag_result = results[swpc.MAG_FIELD.name]
    cycle_result = results[swpc.SOLAR_CYCLE.name]

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
            month = row.get("time-tag")
            weather["solar_cycle_month"] = month if isinstance(month, str) else None

    return weather, reports


# --------------------------------------------------------------------------
# Stage 2: satellites
# --------------------------------------------------------------------------


def collect_satellites(
    store: Store,
    ts,
    *,
    clock: Callable[[], datetime] = utcnow,
    not_after: float | None = None,
) -> tuple[list, list[health.SourceHealth], list[dict]]:
    """Load every tracked group and build propagatable satellites.

    Returns (tracked_satellites, source_health, group_coverage). A group that
    fails contributes zero satellites and one FAILED health entry -- it does
    not stop the others.

    ``group_coverage`` records how many objects each group offered versus how
    many we actually propagate, so the page can state its own sampling rather
    than implying it tracks everything.

    Most runs contact Celestrak for nothing at all: each group is asked at
    most once per publication interval, and in between the stored copy is
    used. That is decided inside :func:`fetch_source` from stored state.
    """
    tracked: list = []
    reports: list[health.SourceHealth] = []
    coverage: list[dict] = []
    budget = NetworkBudget(CELESTRAK_NETWORK_BUDGET_SECONDS, not_after=not_after)

    for group in TRACKED_GROUPS:
        spec = celestrak.group_spec(group)
        result = fetch_source(
            spec, store, deadline=budget.deadline_for(spec.max_seconds), clock=clock
        )
        reports.append(
            health.evaluate(
                result, TLE_STALENESS, label=SOURCE_LABELS[spec.name], now=clock()
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

        # What is stored is already trimmed to the objects we track, so its
        # length is not the size of the group. The fetch recorded that
        # separately; without it the sampling disclosure would turn into a
        # false "all 60 starlink".
        available = result.facts.get("available")
        if not isinstance(available, int) or available < len(result.data):
            available = len(result.data)
        built = build_satellites(celestrak.select_objects(result.data, group), group, ts)
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


def build_correlation(weather: dict, *, kp_current: bool = True) -> dict:
    """Decide whether to raise the operational banner, and say why.

    ``kp_current`` is false when the Kp source has aged past its limit. The
    banner speaks in the present tense -- "storm in progress" -- and a Kp
    value the health panel calls not operationally current is not evidence
    about the present. In that case no assessment is made, and the banner
    says what the last value was and that it is too old to act on.
    """
    if not weather.get("available"):
        return {
            "active": False,
            "severity": "unknown",
            "headline": "Space weather unavailable",
            "detail": (
                "Kp index could not be retrieved, so no geomagnetic assessment "
                "can be made. Pass predictions below are unaffected, but treat "
                "drag-driven error growth as unquantified."
            ),
            "explanation": CORRELATION_EXPLANATION,
            "operational_impacts": [],
            "source": swpc.KP.name,
        }

    kp = weather["kp"]

    if not kp_current:
        return {
            "active": False,
            "severity": "unknown",
            "headline": f"Geomagnetic assessment not current — last Kp {kp:g}",
            "detail": (
                f"The last Kp value received was {kp:g}, and its source has "
                f"gone unanswered for longer than the limit shown in system "
                f"health. That is too old to describe conditions now, so no "
                f"assessment is made from it. Pass predictions below are "
                f"unaffected, but treat drag-driven error growth as "
                f"unquantified."
            ),
            "explanation": CORRELATION_EXPLANATION,
            "operational_impacts": [],
            "source": swpc.KP.name,
        }

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
            "source": swpc.KP.name,
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
            "source": swpc.KP.name,
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
        "source": swpc.KP.name,
    }


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------


def _track_to_dict(track: Track) -> dict:
    return {
        "name": track.name,
        "norad_id": track.norad_id,
        "group": track.group,
        "tle_epoch": track.tle_epoch.isoformat(),
        "track": [list(sample) for sample in track.samples],
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


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def build_document(
    store: Store,
    *,
    observer: Observer = DEFAULT_OBSERVER,
    clock: Callable[[], datetime] = utcnow,
    deadline: Deadline | None = None,
) -> dict:
    """Run one refresh cycle against ``store`` and return the document.

    Does not publish. Never raises on upstream trouble: each stage is
    individually contained, because ``fetch_source`` never raising does not
    stop the code that *interprets* its results from raising -- a stored
    payload whose shape predates the current parser, an upstream that changed
    structure. No single stage is allowed to be the thing that stops the run.
    """
    deadline = deadline or Deadline()
    started_monotonic = time.monotonic()

    # The instant the whole document describes: positions are tracked from
    # it, passes are searched from it, and readers measure the document's age
    # from it. Source health is the one thing measured later, against the
    # clock at the moment each fetch returns -- measuring a source fetched
    # during the run against the run's *start* gives it a negative age, which
    # is how "in the future ago" once reached production.
    generated_at = clock()
    log.info(
        "run start observer=%s lat=%.4f lon=%.4f",
        observer.name,
        observer.latitude_deg,
        observer.longitude_deg,
    )

    ts = timescale()
    site = site_from(observer)

    # Space weather first. Its four reads are small and quick; Celestrak's are
    # the ones that hang when it throttles, and they should not delay these.
    network_ends = deadline.network_must_end_by()
    try:
        weather, weather_reports = collect_space_weather(
            store, clock=clock, not_after=network_ends
        )
    except Exception:
        log.exception("space weather collection failed; continuing without it")
        weather = empty_weather()
        weather_reports = _stage_failed(
            [swpc.KP, swpc.SOLAR_WIND, swpc.MAG_FIELD, swpc.SOLAR_CYCLE],
            SPACE_WEATHER_STALENESS,
            clock(),
        )

    try:
        tracked, tle_reports, coverage = collect_satellites(
            store, ts, clock=clock, not_after=network_ends
        )
    except Exception:
        log.exception("satellite collection failed; continuing with no satellites")
        tracked, coverage = [], []
        tle_reports = _stage_failed(
            [celestrak.group_spec(group) for group in TRACKED_GROUPS],
            TLE_STALENESS,
            clock(),
        )

    # Propagation is pure computation on data we already hold, but a
    # pathological element set could still throw. The document is worth more
    # than any one panel, so a propagation failure degrades to empty lists.
    try:
        tracks = ephemeris(tracked, site, ts, generated_at)
    except Exception:
        log.exception("ephemeris computation failed; continuing with an empty sky")
        tracks = []

    try:
        above_now = len(sky_view(tracked, site, ts, generated_at))
    except Exception:
        log.exception("sky view computation failed; reporting no objects above")
        above_now = 0

    try:
        search = search_passes(
            tracked, site, ts, generated_at, out_of_time=deadline.too_late_to_compute
        )
    except Exception:
        log.exception("pass computation failed; continuing with no passes")
        search = PassSearch(passes=[], searched=0, total=len(tracked))
    # A pass with a number JSON cannot carry would stop the whole document at
    # validation. It costs that pass instead.
    upcoming = [
        item
        for item in search.passes
        if math.isfinite(item.peak_elevation_deg) and math.isfinite(item.duration_seconds)
    ][:MAX_PASSES_TOTAL]

    # The banner speaks about now; a Kp source past its limit cannot.
    kp_current = not any(
        report.name == swpc.KP.name and report.state is health.State.FAILED
        for report in weather_reports
    )

    reports = _in_display_order(tle_reports + weather_reports)
    health_block = health.summarize(reports)

    samples = int(EPHEMERIS_WINDOW / EPHEMERIS_STEP) + 1
    document = {
        "kind": DOCUMENT_KIND,
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "meta": {
            "observer": {
                "name": observer.name,
                "latitude_deg": observer.latitude_deg,
                "longitude_deg": observer.longitude_deg,
                "elevation_m": observer.elevation_m,
            },
            # The schedule the reader needs in order to judge this document's
            # age for itself: how often a new one should appear, and after how
            # long without one something is wrong.
            "pipeline": {
                "cadence_seconds": REFRESH_CADENCE.total_seconds(),
                "silence_limit_seconds": PIPELINE_SILENCE_LIMIT.total_seconds(),
                "run_seconds": round(time.monotonic() - started_monotonic, 2),
                "revision": os.environ.get(REVISION_ENV) or None,
            },
            "parameters": {
                "horizon_elevation_deg": HORIZON_ELEVATION_DEG,
                "min_pass_elevation_deg": MIN_PASS_ELEVATION_DEG,
                "pass_search_hours": PASS_SEARCH_HOURS,
                "celestrak_publication_seconds": (
                    CELESTRAK_PUBLICATION_INTERVAL.total_seconds()
                ),
                "celestrak_min_refetch_seconds": (
                    CELESTRAK_MIN_REFETCH_INTERVAL.total_seconds()
                ),
                "celestrak_retry_seconds": CELESTRAK_RETRY_INTERVAL.total_seconds(),
                "kp_elevated_threshold": KP_ELEVATED_THRESHOLD,
                "kp_storm_threshold": KP_STORM_THRESHOLD,
            },
            "coverage": coverage,
            "counts": {
                "tracked_satellites": len(tracked),
                "above_horizon_at_generation": above_now,
                "upcoming_passes": len(upcoming),
            },
        },
        "sky": {
            "horizon_elevation_deg": HORIZON_ELEVATION_DEG,
            "ephemeris": {
                "start": generated_at.isoformat(),
                "end": (generated_at + EPHEMERIS_STEP * (samples - 1)).isoformat(),
                "step_seconds": EPHEMERIS_STEP.total_seconds(),
                "samples": samples,
                "sample_fields": ["east_km", "north_km", "up_km", "altitude_km"],
            },
            "satellites": [_track_to_dict(track) for track in tracks],
        },
        "passes": {
            "min_elevation_deg": MIN_PASS_ELEVATION_DEG,
            "search_hours": PASS_SEARCH_HOURS,
            "complete": search.complete,
            "satellites_searched": search.searched,
            "satellites_total": search.total,
            "passes": [_pass_to_dict(item) for item in upcoming],
        },
        "space_weather": {
            "weather": weather,
            "correlation": build_correlation(weather, kp_current=kp_current),
        },
        "health": health_block,
    }

    log.info(
        "run computed elapsed=%.2fs tracked=%d tracks=%d passes=%d health=%s",
        time.monotonic() - started_monotonic,
        len(tracked),
        len(tracks),
        len(upcoming),
        health_block["overall"],
    )
    degraded = [s for s in reports if s.state != health.State.FRESH]
    if degraded:
        log.warning(
            "%d/%d source(s) degraded: %s",
            len(degraded),
            len(reports),
            ", ".join(f"{s.name}={s.state.value}" for s in degraded),
        )
    return document


def _stage_failed(
    specs: list[SourceSpec], policy: StalenessPolicy, now: datetime
) -> list[health.SourceHealth]:
    """Health entries for a stage that crashed in our own code.

    The panel must still account for every source. A stage that fell over
    produced nothing usable for any of its sources, so each is reported
    FAILED with the reason -- which is the truth, and keeps the document
    complete enough to publish instead of taking the other panels down too.
    """
    return [
        health.evaluate(
            FetchResult(
                name=spec.name,
                url=spec.url,
                data=None,
                outcome=Outcome.FAILED,
                last_success=None,
                elapsed_seconds=0.0,
                error="Internal error while processing this source (see job logs).",
            ),
            policy,
            label=SOURCE_LABELS[spec.name],
            now=now,
        )
        for spec in specs
    ]


def _in_display_order(reports: list[health.SourceHealth]) -> list[health.SourceHealth]:
    order = {name: index for index, name in enumerate(SOURCE_LABELS)}
    return sorted(reports, key=lambda report: order.get(report.name, len(order)))
