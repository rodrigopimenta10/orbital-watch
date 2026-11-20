"""Configuration for Orbital Watch.

Every tunable lives here as a named constant. The health thresholds in
particular are derived from stated facts -- how often the job runs and how
often each upstream publishes -- and are shown to the reader in the health
panel, so they must be inspectable values rather than numbers buried in logic.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

# --------------------------------------------------------------------------
# Paths (local runs only; the deployed job keeps everything in Cloud Storage)
# --------------------------------------------------------------------------

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent.parent

#: Frontend source. Copied verbatim into the site output.
WEB_DIR = REPO_ROOT / "web"

#: Site output for a local run: the frontend plus a baked copy of the document.
#: This is also what the deploy workflow uploads to Firebase Hosting.
BUILD_DIR = Path(os.environ.get("ORBITAL_WATCH_BUILD", REPO_ROOT / "dist"))

#: Where a local run keeps what the deployed job keeps in its two buckets:
#: ``state/`` for per-source state and ``public/`` for the published document.
LOCAL_DIR = Path(os.environ.get("ORBITAL_WATCH_LOCAL", REPO_ROOT / ".local"))

#: Environment variables that switch the job to Cloud Storage. Both or neither.
STATE_BUCKET_ENV = "ORBITAL_WATCH_STATE_BUCKET"
PUBLIC_BUCKET_ENV = "ORBITAL_WATCH_PUBLIC_BUCKET"

#: Set by the container image build to the commit it was built from, so the
#: published document says which code produced it.
REVISION_ENV = "ORBITAL_WATCH_REVISION"

#: Object name of the one document the job publishes.
DOCUMENT_KEY = "latest.json"

#: Where the site keeps the copy of the document baked in at deploy time.
BAKED_DOCUMENT_PATH = "data/latest.json"

#: Where a local site build puts its stand-in for the public bucket.
LOCAL_LIVE_DOCUMENT_PATH = "live/latest.json"


# --------------------------------------------------------------------------
# Observer
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Observer:
    """A ground station location."""

    name: str
    latitude_deg: float
    longitude_deg: float
    elevation_m: float = 0.0


def _env_float(name: str, default: float, *, low: float, high: float) -> float:
    """Read a float from the environment, falling back loudly on bad input.

    This runs at import time, before logging is configured, so a bare
    ``float()`` here would abort the run with an unexplained ValueError
    traceback -- a mistyped coordinate should not look like a crash. Out-of-
    range values are rejected too: a longitude of 771528 (a missed decimal
    point) would otherwise silently produce a site on the wrong side of the
    planet and passes that are quietly nonsense.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        warnings.warn(
            f"{name}={raw!r} is not a number; using default {default}",
            RuntimeWarning,
            stacklevel=2,
        )
        return default
    if not low <= value <= high:
        warnings.warn(
            f"{name}={value} is outside [{low}, {high}]; using default {default}",
            RuntimeWarning,
            stacklevel=2,
        )
        return default
    return value


#: Default observer. Overridable via environment so the same job can be
#: pointed at a different ground station without a code change.
DEFAULT_OBSERVER = Observer(
    name=os.environ.get("ORBITAL_WATCH_SITE_NAME", "Rockville, MD"),
    latitude_deg=_env_float("ORBITAL_WATCH_LAT", 39.0840, low=-90.0, high=90.0),
    longitude_deg=_env_float("ORBITAL_WATCH_LON", -77.1528, low=-180.0, high=180.0),
    elevation_m=_env_float("ORBITAL_WATCH_ELEV_M", 82.0, low=-500.0, high=9000.0),
)


# --------------------------------------------------------------------------
# The pipeline's own schedule
# --------------------------------------------------------------------------

#: Name of the environment variable through which the deployment tells the job
#: how often it is started. Terraform sets the Cloud Scheduler cron and this
#: variable from the same value (``refresh_cadence_minutes``), so the cadence
#: the code reasons about is the cadence it is actually run at.
CADENCE_ENV = "ORBITAL_WATCH_CADENCE_MINUTES"

#: Cadence assumed when nothing sets the variable (local runs, tests). A test
#: pins this to the Terraform default so the two cannot drift apart.
DEFAULT_REFRESH_CADENCE_MINUTES = 10


def _refresh_cadence() -> timedelta:
    minutes = _env_float(
        CADENCE_ENV, float(DEFAULT_REFRESH_CADENCE_MINUTES), low=1.0, high=1440.0
    )
    return timedelta(minutes=minutes)


#: How often the job runs. Every other time constant below that depends on
#: scheduling is written in terms of this one.
REFRESH_CADENCE = _refresh_cadence()

#: Runs in a row that may go missing before anyone is told. One missed run is
#: an ordinary event (a slow start, a deploy); three in a row is not.
MISSED_RUNS_BEFORE_ALARM = 3

#: After this long without a new document the pipeline counts as silent: the
#: page tells the visitor, and the "no successful run" alert tells the owner
#: (Terraform derives the same figure from the same cadence).
PIPELINE_SILENCE_LIMIT = REFRESH_CADENCE * MISSED_RUNS_BEFORE_ALARM

#: Wall-clock allowance for one run, from start to published document. Well
#: under the cadence, so one run has finished before the next starts; the
#: Cloud Run task timeout sits above it as the hard stop.
RUN_DEADLINE_SECONDS = 180.0

#: Time kept back from that allowance for work that comes after the network:
#: propagating and searching for passes, then validating and publishing.
#: Network requests may not begin to use it, whatever the upstreams do.
COMPUTE_RESERVE_SECONDS = 20.0
PUBLISH_RESERVE_SECONDS = 30.0


# --------------------------------------------------------------------------
# Upstream sources
# --------------------------------------------------------------------------

USER_AGENT = (
    "OrbitalWatch/2.0 (+https://github.com/rodrigopimenta10/orbital-watch) "
    "portfolio project; contact via GitHub issues"
)

#: Longest a connection may go without sending anything: while connecting,
#: while waiting for the headers, or between two pieces of a body. This is
#: the socket timeout. It catches an upstream that has gone silent; it cannot
#: catch one that is merely slow, which is what the ceilings below are for.
HTTP_STALL_TIMEOUT_SECONDS = 10.0

#: Ceiling on one whole request, from sending it to the last byte of the
#: body. No unbounded waits, ever. Every source here but one answers in well
#: under a second with at most half a megabyte.
HTTP_TIMEOUT_SECONDS = 20.0

#: The largest response is the group Starlink is taken from: under 100 KB
#: since §31 (it was the full 5 MB Starlink group before). It keeps a longer
#: ceiling than the others because Celestrak has been seen serving at a few
#: kilobytes a second on a bad day (3.4 KB/s for this file on 2026-11-20, so
#: 25 s), and one slow answer every two hours is better than none. Anything
#: slower than this is abandoned, reported, and retried in half an hour.
STARLINK_TIMEOUT_SECONDS = 45.0

#: Ceilings on total network time per upstream provider in one run, kept
#: separate so that one provider being slow cannot spend the other's
#: allowance. Per-request ceilings alone are not enough: several sources each
#: using theirs in full serialises into minutes of waiting before propagation
#: starts. Once a budget is spent, remaining requests to that provider get a
#: second each, so a run in which everything is down still finishes promptly.
#: SWPC's covers three of its four small products timing out outright;
#: Celestrak's covers the Starlink ceiling plus one more group's.
SWPC_NETWORK_BUDGET_SECONDS = 60.0
CELESTRAK_NETWORK_BUDGET_SECONDS = 65.0

#: The default for a budget built without a figure.
NETWORK_BUDGET_SECONDS = SWPC_NETWORK_BUDGET_SECONDS

CELESTRAK_GP_URL = "https://celestrak.org/NORAD/elements/gp.php"

#: Celestrak regenerates GP data about every two hours and says so in its own
#: "not updated" reply. This is a fact about the upstream, not a setting.
CELESTRAK_PUBLICATION_INTERVAL = timedelta(hours=2)

#: The job never asks Celestrak for a group again within this long of the last
#: time Celestrak answered for it. Equal to the publication interval: asking
#: sooner cannot return newer element sets, and Celestrak rate-limits callers
#: who try. Enforced against the stored state, so it holds however often the
#: job is started.
CELESTRAK_MIN_REFETCH_INTERVAL = CELESTRAK_PUBLICATION_INTERVAL

#: After a failed attempt the job waits this long before trying that group
#: again, instead of hammering a struggling or rate-limiting server on every
#: run. Three cadences: short enough to recover well inside the freshness
#: window, long enough that an outage costs Celestrak two requests per group
#: an hour at the default cadence, not six.
CELESTRAK_RETRY_INTERVAL = PIPELINE_SILENCE_LIMIT

#: NOAA SWPC products are small and refreshed by the minute; they are fetched
#: on every run with no floor. Kp itself is a three-hourly index.
KP_PUBLICATION_INTERVAL = timedelta(hours=3)


@dataclass(frozen=True)
class SatelliteGroup:
    """A Celestrak GP group we track."""

    key: str
    label: str
    #: Cap on how many objects from this group we propagate. Starlink alone is
    #: ~11k objects; propagating all of them would blow up run time and the
    #: published document for no analytical gain. See DECISIONS.md.
    max_objects: int
    #: Ceiling on one whole request for this group, body included.
    fetch_seconds: float = HTTP_TIMEOUT_SECONDS
    #: The Celestrak GROUP to ask for, when it is not the key itself.
    query_group: str | None = None
    #: Keep only objects whose name starts with this, when the group asked for
    #: holds more than the objects we want from it.
    name_prefix: str | None = None


TRACKED_GROUPS: tuple[SatelliteGroup, ...] = (
    SatelliteGroup(key="stations", label="Space Stations", max_objects=40),
    SatelliteGroup(key="weather", label="Weather Satellites", max_objects=90),
    # Starlink, taken from Celestrak's "launched in the last 30 days" group
    # (about 200 objects, under 100 KB) and not from the full Starlink group
    # (about 11,000 objects, 5 MB). The page shows sixty of them either way;
    # downloading 5 MB every two hours to show sixty objects cost Celestrak
    # about 60 MB a day, against a published limit of roughly 100 MB a day per
    # address before it firewalls the address. See DECISIONS.md §31.
    SatelliteGroup(
        key="starlink",
        label="Recent Starlink",
        max_objects=60,
        fetch_seconds=STARLINK_TIMEOUT_SECONDS,
        query_group="last-30-days",
        name_prefix="STARLINK",
    ),
)

SWPC_KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
SWPC_SOLAR_CYCLE_URL = (
    "https://services.swpc.noaa.gov/json/solar-cycle/observed-solar-cycle-indices.json"
)
#: NOTE: the /products/solar-wind/plasma-*.json family documented in the brief
#: now returns 404 -- SWPC retired that path. These summary endpoints carry the
#: same headline quantities. See DECISIONS.md.
SWPC_SOLAR_WIND_URL = (
    "https://services.swpc.noaa.gov/products/summary/solar-wind-speed.json"
)
SWPC_MAG_FIELD_URL = (
    "https://services.swpc.noaa.gov/products/summary/solar-wind-mag-field.json"
)


# --------------------------------------------------------------------------
# Health thresholds
# --------------------------------------------------------------------------
#
# A source is FRESH if upstream last answered for it within `fresh_within`,
# STALE if within `stale_within`, and FAILED beyond that (or if it has never
# answered). Both numbers are published in the health panel so a reader can
# check the arithmetic. Each is written as an expression over the facts above
# rather than as a bare number, so changing the cadence moves the thresholds
# with it and there is no pair of constants to keep in step by hand.


@dataclass(frozen=True)
class StalenessPolicy:
    fresh_within: timedelta
    stale_within: timedelta


#: Element sets. A group is asked for every CELESTRAK_MIN_REFETCH_INTERVAL, so
#: in normal running its last answer is never older than that plus one cadence.
#: Fresh therefore means "not more than MISSED_RUNS_BEFORE_ALARM runs overdue".
#: The outer limit is a judgement rather than a derivation: element sets age
#: gracefully, and two days (24 upstream publications) is where this project
#: stops calling predictions made from them current.
TLE_STALENESS = StalenessPolicy(
    fresh_within=CELESTRAK_MIN_REFETCH_INTERVAL + PIPELINE_SILENCE_LIMIT,
    stale_within=CELESTRAK_PUBLICATION_INTERVAL * 24,
)

#: Space weather. Asked for on every run, so anything older than the silence
#: limit means runs are being missed. Past one full Kp interval the newest
#: index value has never been seen, and a solar wind reading that old says
#: nothing about now.
SPACE_WEATHER_STALENESS = StalenessPolicy(
    fresh_within=PIPELINE_SILENCE_LIMIT,
    stale_within=KP_PUBLICATION_INTERVAL,
)


# --------------------------------------------------------------------------
# Propagation / passes
# --------------------------------------------------------------------------

#: Minimum elevation for a pass to count. Below ~10 deg most ground stations
#: are looking through terrain, buildings, and too much atmosphere.
MIN_PASS_ELEVATION_DEG = 10.0

#: Elevation above which we call a satellite "visible now" in the sky view.
HORIZON_ELEVATION_DEG = 0.0

#: How far ahead to search for passes.
PASS_SEARCH_HOURS = 24

#: Passes retained per satellite in the output.
MAX_PASSES_PER_SAT = 3

#: Passes retained overall in the output.
MAX_PASSES_TOTAL = 40

#: Length of the position track published for the sky view. The browser
#: interpolates along it to its own clock, so it has to outlast the wait for
#: the next document. Equal to the silence limit on purpose: the moment the
#: track runs out is the moment the page starts saying the pipeline is silent,
#: so positions are never shown as current after the page has stopped
#: believing the pipeline is alive.
EPHEMERIS_WINDOW = PIPELINE_SILENCE_LIMIT

#: Spacing of the samples in that track. A low-Earth-orbit satellite moves
#: about 230 km in 30 s along a path whose curvature puts the straight line
#: between two samples under 1 km from the true position -- a fraction of a
#: degree at any range a ground station sees. A test holds the published
#: track to that.
EPHEMERIS_STEP = timedelta(seconds=30)

#: An object gets a track only if it comes within this many degrees of the
#: horizon during the window. The margin below zero is there so the samples on
#: either side of a rise or a set are always present to interpolate between.
EPHEMERIS_HORIZON_MARGIN_DEG = 2.0


# --------------------------------------------------------------------------
# Space weather interpretation
# --------------------------------------------------------------------------

#: Kp at or above this is a geomagnetic storm (NOAA G1+).
KP_STORM_THRESHOLD = 5.0

#: Kp at or above this is "active" -- the point where we raise the operational
#: correlation banner, because this is where ground-segment teams start
#: caring about drag-induced tracking error and HF degradation.
KP_ELEVATED_THRESHOLD = 4.0
