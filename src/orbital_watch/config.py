"""Configuration for Orbital Watch.

Every tunable lives here as a named constant. Staleness thresholds in
particular are deliberately explicit -- the health panel reports them to the
user, so they must be inspectable values rather than magic numbers buried in
logic.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent.parent

#: Disk cache for upstream responses. Machine-local and ephemeral in every
#: CI/build environment this project uses -- which is exactly why the
#: committed seed/ snapshot exists as the durable fallback, and why the
#: snapshot refresher pre-warms this cache from seed/ before fetching.
CACHE_DIR = Path(os.environ.get("ORBITAL_WATCH_CACHE", REPO_ROOT / ".cache"))

#: Frontend source. Copied verbatim into the build output.
WEB_DIR = REPO_ROOT / "web"

#: Build output. Static files + generated JSON; this is what gets deployed.
BUILD_DIR = Path(os.environ.get("ORBITAL_WATCH_BUILD", REPO_ROOT / "dist"))

#: Committed last-resort snapshot, used when there is no cache AND upstream
#: is unreachable. Build environments without persistent disk (Cloudflare
#: Pages containers are ephemeral) start cold every time, so without this a
#: single upstream hiccup during a deploy would publish an empty site.
#: Its manifest records each snapshot's true capture time, so the health
#: panel ages it honestly rather than presenting it as current.
SEED_DIR = Path(os.environ.get("ORBITAL_WATCH_SEED", REPO_ROOT / "seed"))

#: Generated JSON lands here, under the build dir.
DATA_SUBDIR = "data"


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
    ``float()`` here would abort the build with an unexplained ValueError
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


#: Default observer. Overridable via environment so the same build can be
#: pointed at a different ground station without a code change.
DEFAULT_OBSERVER = Observer(
    name=os.environ.get("ORBITAL_WATCH_SITE_NAME", "Rockville, MD"),
    latitude_deg=_env_float("ORBITAL_WATCH_LAT", 39.0840, low=-90.0, high=90.0),
    longitude_deg=_env_float("ORBITAL_WATCH_LON", -77.1528, low=-180.0, high=180.0),
    elevation_m=_env_float("ORBITAL_WATCH_ELEV_M", 82.0, low=-500.0, high=9000.0),
)


# --------------------------------------------------------------------------
# Upstream sources
# --------------------------------------------------------------------------

USER_AGENT = (
    "OrbitalWatch/1.0 (+https://github.com/rodrigopimenta10/orbital-watch) "
    "portfolio project; contact via GitHub issues"
)

#: Hard ceiling on any single network call. No unbounded waits, ever.
HTTP_TIMEOUT_SECONDS = 20.0

#: Ceiling on total network wait across the whole build. Per-call timeouts
#: alone are not enough: with every source down, N sources x 20s serialises
#: into minutes of waiting before propagation even starts. Once this budget is
#: spent, remaining fetches get whatever is left (minimum 1s), so a
#: degraded-everything build still finishes promptly. The value covers all
#: SWPC sources timing out fully with room to spare -- it only bites when
#: several sources hang in sequence, which is exactly the case it exists for.
NETWORK_BUDGET_SECONDS = 60.0

CELESTRAK_GP_URL = "https://celestrak.org/NORAD/elements/gp.php"

#: Celestrak refreshes GP data every 2 hours and rate-limits repeat callers.
#: We refuse to re-fetch a group whose cache is younger than this, regardless
#: of how often the build runs. See DECISIONS.md.
CELESTRAK_MIN_REFETCH_INTERVAL = timedelta(hours=6)


@dataclass(frozen=True)
class SatelliteGroup:
    """A Celestrak GP group we track."""

    key: str
    label: str
    #: Cap on how many objects from this group we propagate. Starlink alone is
    #: ~11k objects; propagating all of them would blow up build time and the
    #: output JSON for no analytical gain. See DECISIONS.md.
    max_objects: int


TRACKED_GROUPS: tuple[SatelliteGroup, ...] = (
    SatelliteGroup(key="stations", label="Space Stations", max_objects=40),
    SatelliteGroup(key="weather", label="Weather Satellites", max_objects=90),
    SatelliteGroup(key="starlink", label="Starlink", max_objects=60),
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
# Staleness thresholds
# --------------------------------------------------------------------------
#
# A source is FRESH if its last successful fetch is within `fresh_within`,
# STALE if within `stale_within`, and FAILED beyond that (or if it has never
# succeeded). These are surfaced verbatim in the health panel so a viewer can
# check our arithmetic.


@dataclass(frozen=True)
class StalenessPolicy:
    fresh_within: timedelta
    stale_within: timedelta


#: TLEs age slowly -- a 6-hour-old element set is operationally fine, and SGP4
#: accuracy degrades gradually rather than falling off a cliff.
TLE_STALENESS = StalenessPolicy(
    fresh_within=timedelta(hours=8),
    stale_within=timedelta(hours=48),
)

#: Space weather is the opposite: a 3-hour-old Kp is the current Kp, but a
#: day-old one tells you nothing about right now.
SPACE_WEATHER_STALENESS = StalenessPolicy(
    fresh_within=timedelta(hours=3),
    stale_within=timedelta(hours=12),
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


# --------------------------------------------------------------------------
# Space weather interpretation
# --------------------------------------------------------------------------

#: Kp at or above this is a geomagnetic storm (NOAA G1+).
KP_STORM_THRESHOLD = 5.0

#: Kp at or above this is "active" -- the point where we raise the operational
#: correlation banner, because this is where ground-segment teams start
#: caring about drag-induced tracking error and HF degradation.
KP_ELEVATED_THRESHOLD = 4.0
