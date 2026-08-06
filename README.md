# Orbital Watch

A public dashboard that tracks which satellites are passing over a ground
station and correlates those passes with live space-weather conditions.

**Live site:** _not yet deployed — see [Deployment](#deployment). Once GitHub
Pages is enabled the URL is `https://<user>.github.io/orbital-watch/`._

![The Orbital Watch dashboard: operational assessment banner, space weather tiles, and Kp history chart](docs/dashboard.jpg)

---

## Why it exists

Ground-segment teams care about two things that look unrelated and are not.

The first is **when a spacecraft is reachable**. Passes over a station are
short — a low-Earth-orbit satellite is above a 10° mask for roughly five to
twelve minutes — and everything else in the schedule bends around them.

The second is **geomagnetic activity**, and it is not a curiosity. Energy
deposited into the upper atmosphere during a geomagnetic storm heats and
expands the thermosphere, raising neutral density at LEO altitudes. Higher
density means more drag: satellites lose altitude faster, along-track position
error grows between element-set updates, and published TLEs go stale sooner
than usual. The same disturbance degrades the links used to talk to those
spacecraft — HF propagation becomes unreliable at high latitudes, and
ionospheric scintillation can disrupt UHF and L-band, including GPS.

So when Kp climbs, a tracking schedule computed from yesterday's elements gets
quietly less accurate at the same moment the links get less reliable. This
dashboard puts both on one screen and states the operational consequence
explicitly rather than leaving the reader to connect them.

It is a portfolio project, not an operational tool.

---

## Architecture

```
   ┌─────────────────┐        ┌──────────────────┐
   │  Celestrak GP   │        │    NOAA SWPC     │      upstream, public,
   │  (TLE/OMM)      │        │  (Kp, wind, IMF) │      unauthenticated
   └────────┬────────┘        └────────┬─────────┘
            │                          │
            └──────────┬───────────────┘
                       ▼
         ┌──────────────────────────────┐
         │  scheduled GitHub Action     │   hourly
         │  fetch → cache → propagate   │
         │  → health → write JSON       │
         └──────────────┬───────────────┘
                        ▼
         ┌──────────────────────────────┐
         │  static JSON + HTML/CSS/JS   │   dist/
         └──────────────┬───────────────┘
                        ▼
                 GitHub Pages
                        │
                        ▼
                    browser          ← never calls an upstream API
```

A scheduled Python job fetches upstream data, computes positions and passes,
evaluates the health of every source, and writes five JSON files into the build
output. The frontend loads those files and renders. **The browser never
contacts Celestrak or NOAA.**

That indirection is the point, and it buys three things:

1. **An upstream outage cannot take the site down.** If Celestrak is
   unreachable at 03:00, the scheduled run falls back to cached element sets,
   the site still renders every panel, and the health panel says the data is
   stale and why. Compare a live-proxy design, where the same outage produces
   a spinner or a stack trace in front of whoever is looking.
2. **Hosting costs nothing and cannot fall over.** Static files on a CDN. No
   server, no serverless function, no database, no secrets.
3. **"Last updated" becomes a real claim.** Because every fetch is recorded
   with its outcome and timestamp, the freshness shown on the page is measured
   rather than asserted.

---

## Reliability behaviour

This is the part worth reading closely, because it is the part the project is
actually about.

### Every network call goes through one function

There is exactly one place in the codebase that performs network I/O:
`fetch_json()` in `src/orbital_watch/sources/fetch.py`. It guarantees four
things for every caller:

- a hard timeout — no unbounded waits;
- failure is caught, logged with source and elapsed time, and falls back to the
  on-disk cache;
- the outcome is recorded in a result object the health panel reports verbatim;
- **it never raises.** No caller can abort the build by forgetting a
  `try`/`except`, because there is nothing to catch.

A single choke point rather than scattered `try`/`except` blocks means the
question "what happens when upstream is down?" has exactly one answer, and that
answer is testable in isolation.

### What each failure actually does

| Situation | Outcome | Site behaviour | Health panel |
|---|---|---|---|
| Fetch succeeds | `live` | Fresh data | **fresh** |
| Cache younger than the refetch floor | `cache_fresh` | Cached data | **fresh** — upstream deliberately not contacted |
| Upstream says nothing changed | `not_modified` | Cached data | **fresh** |
| Upstream down, cache warm | `cache_fallback` | Cached data still renders | **stale** — with the connection error |
| Upstream down, no cache | `failed` | That panel empties, others unaffected | **failed** — with the reason |
| Data older than the staleness limit | — | Data still renders | **failed** — "not operationally current" |
| Malformed / non-JSON response | `cache_fallback` | Cached data | **stale** — cache is *not* overwritten |

Two behaviours worth calling out:

**A source can be serving good numbers and still not be "fresh."** If upstream
was unreachable this run but the cache is 30 minutes old, the data on screen is
fine — and the panel reports **stale** anyway, because we have lost contact and
saying "fresh" would be a lie. Reporting freshness honestly is the entire
purpose of the panel.

**A bad response never poisons the cache.** A malformed or non-JSON reply is
rejected at the boundary, so the good cached copy you would fall back to
survives. There is a test for exactly this.

### Degradation is proven, not asserted

`tests/test_sources.py` breaks each of the seven sources in turn under multiple
failure modes (timeout, DNS failure, HTTP 500, HTTP 403), then breaks all of
them at once, and asserts every time that the build completes and writes a
complete, parseable site. The full-blackout test additionally asserts that the
health block reports everything failed rather than pretending otherwise.

You can watch it happen for real:

```bash
# Build with all sockets blocked and a cold cache.
uv run python - <<'PY'
import socket
def dead(*a, **k): raise OSError(101, "Network is unreachable")
socket.socket.connect = dead; socket.create_connection = dead; socket.getaddrinfo = dead
from orbital_watch.build import main
raise SystemExit(main(["--build-dir", "dist-offline", "--cache-dir", ".cache-offline"]))
PY
```

This exits 0, writes all five JSON files, and reports 7/7 sources failed. The
same check runs in CI on every push.

---

## Local setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
git clone <repo-url>
cd orbital-watch

# Install dependencies (creates .venv automatically)
uv sync --dev

# Fetch upstream data and build the site into dist/
uv run python -m orbital_watch.build

# Serve it -- the page fetches JSON, so file:// will not work
python3 -m http.server 8000 --directory dist
# then open http://localhost:8000
```

Useful flags:

```bash
uv run python -m orbital_watch.build --verbose          # debug logging
uv run python -m orbital_watch.build --build-dir out    # different output dir
uv run python -m orbital_watch.build --cache-dir /tmp/c # different cache
```

Point it at a different ground station without touching code:

```bash
ORBITAL_WATCH_SITE_NAME="Svalbard" \
ORBITAL_WATCH_LAT=78.2297 \
ORBITAL_WATCH_LON=15.4075 \
  uv run python -m orbital_watch.build
```

---

## Tests

```bash
uv run pytest              # 106 tests
uv run ruff check .        # lint
uv run ruff format --check .
```

**No test makes a network call.** That is enforced mechanically, not by
convention: an autouse fixture in `tests/conftest.py` replaces
`urllib.request.urlopen` with a function that raises, so a live call fails
loudly instead of passing on one machine and flaking in CI. Everything runs
from real API responses captured into `tests/fixtures/`.

Three groups are worth knowing about:

**Propagation correctness** (`test_propagate.py`) checks against the published
SGP4 verification vectors from Vallado et al., *Revisiting Spacetrack Report #3*
(AIAA 2006-6753) — an independently-known result, not a number this project
generated and then froze. Backed by physical checks that nothing in the code
knows the answer to: GOES spacecraft must come out at geostationary altitude
(~35,786 km), the ISS at a plausible LEO altitude with a ~93-minute period.

**Health boundaries** (`test_health.py`) pin every freshness threshold
including the exact-equality cases, where off-by-one errors live.

**Graceful degradation** (`test_sources.py`) is the most important test in the
project. It proves the claim the whole architecture rests on: that no upstream
failure — individually or all at once, cold cache or warm — can prevent the
build from producing a complete site that tells the truth about its own state.

---

## Data sources

| Source | Endpoint | Fetched |
|---|---|---|
| [Celestrak](https://celestrak.org/) | GP API, groups `stations`, `weather`, `starlink` | at most every 6 h |
| [NOAA SWPC](https://www.swpc.noaa.gov/) | planetary K-index | hourly |
| NOAA SWPC | solar wind speed, IMF Bt/Bz | hourly |
| NOAA SWPC | observed solar cycle indices | hourly |

All public, unauthenticated, no API keys. **The project requires no secrets and
no `.env` file of any kind.**

Celestrak rate-limits aggressively and blocks abusive clients, so:

- responses are cached to disk and reused;
- the fetcher **refuses** to re-request a group whose cache is younger than six
  hours, enforced in code rather than by the cron schedule — so the limit holds
  even under manual runs or a misconfigured trigger;
- requests carry a descriptive `User-Agent` identifying the project;
- only the three named groups are fetched, never the full catalogue.

Starlink is sampled (60 of ~10,900 objects, most recent epochs) — see
[DECISIONS.md](DECISIONS.md).

Two upstream findings, both documented in [DECISIONS.md](DECISIONS.md): the
`products/solar-wind/plasma-*.json` endpoints in the original spec now return
404 and were replaced with SWPC's summary endpoints; and Celestrak signals
"nothing has changed" with an **HTTP 403 whose body is a plain sentence**,
which needs handling on the error path or a benign no-op gets reported as a
hard failure.

---

## Deployment

Deploys to GitHub Pages via Actions — artifact upload and `deploy-pages`, not a
`gh-pages` branch (a checked-in `.gitignore` on that branch silently drops
files).

1. Push to GitHub.
2. **Settings → Pages → Source: GitHub Actions.**
3. Run **Update data and deploy** manually once, or wait for the hourly
   schedule.

`.github/workflows/`:

- **`ci.yml`** — `ruff check`, `ruff format --check`, `pytest` on every push
  and PR, plus a step that builds with all sockets blocked and asserts the
  result is a complete site reporting total upstream failure.
- **`update-data.yml`** — hourly fetch, rebuild, and deploy, with
  `workflow_dispatch` for manual runs. Uses `concurrency: pages` with
  `cancel-in-progress: false`, so a half-published site is never the result of
  two overlapping runs. The upstream cache persists between runs via
  `actions/cache` — without it every run would start cold and any outage would
  blank a panel. Per-source health is written to the job summary, since the
  build deliberately does not fail on upstream problems.

---

## What is intentionally not built

Scope discipline is part of the design, so these are omissions, not gaps:

- **No user accounts, no database, no notifications.** All three would require
  a backend, which would forfeit the property that makes this thing reliable.
- **No full satellite catalogue browser.** Celestrak already is one, and doing
  it properly means search, pagination, and a much larger payload.
- **No 3D globe.** It would be the most eye-catching thing here and would tell
  an operator nothing that the elevation, azimuth, and range columns do not
  already say more precisely.
- **No mobile app.** The page is responsive; a native app would add
  distribution overhead for zero functional gain.
- **No live API proxy or serverless function.** Deliberate. It would
  reintroduce exactly the runtime upstream dependency the architecture exists
  to remove.
- **No illumination or eclipse modelling.** It needs a JPL ephemeris kernel —
  a ~16 MB download and another build-time network dependency — for a feature
  outside the brief.

---

## Layout

```
src/orbital_watch/
├── config.py            observer, tracked groups, thresholds — all constants
├── sources/
│   ├── fetch.py         the only network I/O: timeout, cache, status
│   ├── celestrak.py     GP element sets, rate-limit-aware
│   └── swpc.py          space weather + Kp interpretation
├── propagate.py         SGP4 → positions and passes
├── health.py            freshness state machine
└── build.py             entry point: writes dist/data/*.json
web/                     static frontend, copied verbatim into dist/
tests/fixtures/          real API responses, committed
```

Design decisions and their rationale are in [DECISIONS.md](DECISIONS.md).
