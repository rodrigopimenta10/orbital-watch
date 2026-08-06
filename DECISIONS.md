# Decisions

Every open decision from `claude.md`, plus the ones forced by what the upstream
APIs actually did. Each entry records the choice, the reasoning, and what would
change my mind.

---

## 1. Frontend: plain HTML/CSS/JS, no build step

**Decision.** Hand-written HTML, CSS, and one JavaScript file. No framework,
no bundler, no Node at build time or runtime.

**Why.** The brief requires static output and no Node server at runtime. A
framework would satisfy that too, but it buys nothing here and costs
real things:

- The dashboard is five panels of read-only data with one chart. There is no
  client-side routing, no shared mutable state, and no component reuse worth
  abstracting. React would be more lines of configuration than of application.
- A bundler means `node_modules`, a lockfile, and a build step that can rot
  independently of the Python pipeline. For a project whose entire selling
  point is "it keeps working", adding a second toolchain that can break is a
  poor trade.
- `web/` is copied verbatim into `dist/`. What I wrote is what ships, which
  makes the deployed artefact trivially auditable.

**What would change my mind.** Adding interactive filtering, a second page, or
any client-side state machine. At that point hand-rolled DOM code stops being
simpler than a framework.

---

## 2. Propagation library: `skyfield` (over raw `sgp4`)

**Decision.** `skyfield`, which wraps the reference `sgp4` implementation.

**Why.** The hard part of this project is not propagating a state vector — it
is pass prediction: finding rise, culmination, and set times against an
elevation mask, from a topocentric frame, on a WGS84 ellipsoid. `skyfield`
provides `find_events()` and `altaz()` for exactly that. Implementing
culmination search on raw `sgp4` means writing a root-finder over an elevation
function and getting the TEME→ITRF→topocentric transforms right by hand. That
is a well-known source of subtle, hard-to-test bugs, and re-deriving it would
demonstrate stubbornness rather than judgement.

`skyfield` also does not cost accuracy: it calls the same `sgp4` routines
underneath, which is why the verification test in `tests/test_propagate.py`
can check against the published Vallado vectors and match them to within
1 mm in position and 10 µm/s in velocity.

**Deliberate limitation.** I use only topocentric satellite geometry, which
needs no planetary ephemeris. Anything involving the Sun — visual-illumination
passes, eclipse entry — would require the JPL DE kernel, a ~16 MB download at
build time and another network dependency that can fail. Not worth it for a
feature the brief does not ask for. This is why `load.timescale(builtin=True)`
is used explicitly: it guarantees no network access during propagation.

---

## 3. Update cadence: hourly workflow, 6-hour Celestrak floor enforced in code

**Decision.** The scheduled workflow runs hourly (`0 * * * *`). The Celestrak
fetcher independently refuses to re-request a group whose cached copy is
younger than 6 hours.

**Why these are two separate numbers.** This is the decision I care most about
in this file. Tying politeness to the cron schedule is fragile: someone
triggers a few manual runs, or adds a second trigger, and suddenly you are
hammering a free public service. Enforcing the floor *in the fetcher* means
the rate limit holds no matter how often the build is invoked — including
`workflow_dispatch`, including local development, including a misconfigured
schedule.

The cadences themselves:

- **SWPC hourly.** The config gives space weather a 3-hour freshness window.
  Hourly keeps it comfortably fresh with room to miss a run or two before the
  site reports stale. Kp is a 3-hourly index, but solar wind speed updates by
  the minute, so hourly is not wasted.
- **Celestrak every 6 hours at most.** Celestrak regenerates GP data every
  2 hours and TLEs do not meaningfully change faster than that. 6 hours keeps
  element sets well inside their 8-hour freshness window while costing
  Celestrak at most 4 requests per group per day. Given they rate-limit and
  block abusive clients — and, as documented below, did in fact throttle me
  during development — being conservative here is correct.

---

## 4. Starlink is sampled, not tracked wholesale

**Decision.** Each group is capped (`SatelliteGroup.max_objects`); Starlink is
capped at 60 of ~10,900 objects, keeping the most recent epochs.

**Why.** The live Starlink group returned **10,894 objects / 4.6 MB**.
Propagating all of them and running a 24-hour pass search over each would
dominate build time and produce an output file no browser should be asked to
parse — for no analytical gain. The dashboard's job is representative
visibility over one ground station; a full catalogue browser is explicitly out
of scope (brief §4.2).

Most-recent-epoch is the selection rule because those element sets carry the
least propagation error.

**Honest limitation, stated on the page.** This means "next passes" is the
next pass among *tracked* objects, not among every object in orbit. The page
says so itself, with real numbers, under both tables: the build publishes
per-group coverage in `meta.json` and the frontend renders it — currently
"all 22 space stations, all 74 weather satellites, 60 of 10,894 starlink".

Only Starlink is actually sampled. The stations and weather caps sit above
the live group sizes, so those are complete. (An earlier revision capped
weather at 60 against a live group of 74, silently dropping 14 while the
documentation claimed only Starlink was sampled. The cap is now 90, and the
page publishes the numbers so the claim is checkable rather than trusted.)

---

## 5. SWPC solar-wind endpoint replaced (upstream changed)

**Decision.** Use
`https://services.swpc.noaa.gov/products/summary/solar-wind-speed.json` and
`.../summary/solar-wind-mag-field.json` instead of the
`products/solar-wind/plasma-2-hour.json` named in the brief.

**Why.** The endpoint in the brief returns **404**. So does every variant I
probed — `plasma-5-minute`, `plasma-1-day`, `plasma-6-hour`, `plasma-7-day`.
NOAA appears to have retired that path entirely.

The summary endpoints are live, unauthenticated, and carry the same headline
quantities the panel needs:

```json
[{"proton_speed": 301, "time_tag": "2026-08-06T14:45:00Z"}]
[{"bt": 2, "bz_gsm": -1, "time_tag": "2026-08-06T14:46:00Z"}]
```

I also surface **Bz** (north–south IMF component), which the brief does not ask
for. It is the single most operationally meaningful solar-wind number besides
speed: sustained southward Bz is what actually couples solar-wind energy into
the magnetosphere and drives the geomagnetic activity this dashboard is about.

---

## 6. Celestrak's "not updated" reply is HTTP 403, not 200

**Decision.** Detect the sentinel body on **both** the success path and the
`HTTPError` path, and treat it as `NOT_MODIFIED` (serve cache, stay healthy)
rather than a failure.

**Why.** Discovered against the live API. Re-requesting a GP group inside its
refresh window returns:

```
HTTP/1.1 403 Forbidden

GP data has not updated since your last successful
download of GROUP=starlink at 2026-08-06 14:52:37 UTC.
Data is updated once every 2 hours.
```

Two traps here, and I fell into the second one before catching it:

1. **It is not JSON.** A naive `json.loads` either crashes the build or — far
   worse — writes that sentence into the cache, destroying the good data you
   would otherwise fall back to. There is a test asserting the cache still
   holds JSON after this response.
2. **It is not a 2xx.** My first implementation only checked the body on the
   success path, so the 403 raised before the check could run and a completely
   benign "nothing has changed" was reported to the health panel as a hard
   failure — while a perfectly good cache sat unused. `urllib` only exposes
   the body via `HTTPError.read()`, so the error path has to look too.

Only the sentinel body is forgiven; a genuine 403 still degrades. Both cases
are pinned by tests.

---

## 7. HTTP via stdlib `urllib`, not `requests`

**Decision.** No HTTP library dependency. `skyfield` is the only runtime
dependency (pulling `numpy`, `sgp4`, `jplephem`, `certifi`).

**Why.** The brief asks for minimal, justified dependencies. This project makes
seven GETs with a timeout and a User-Agent header — the exact intersection of
`urllib` and `requests` capability. `requests` would be a dependency added for
ergonomics I do not need in ~200 lines of fetching code. Its one real advantage
here, connection pooling, is irrelevant across seven one-shot requests to four
hosts.

---

## 8. A source can be serving good data and still not be "fresh"

**Decision.** `Outcome.CACHE_FALLBACK` with young data classifies as
**stale**, not fresh. Age alone does not determine state.

**Why.** This is the judgement call the health panel exists to make. If
upstream was unreachable this run but the cache is 30 minutes old, the numbers
on screen are perfectly good — and reporting "fresh" would still be a lie,
because we have lost contact and nobody would know until the data aged out
hours later. The panel's whole purpose is that the state means what it says.

Related fix, found by a test: when a source was *both* too old and unreachable,
the detail message reported only the age and silently dropped the connectivity
loss — the more actionable half. It now reports both.

The precedence rules and every threshold boundary, including exact-equality
cases, are pinned in `tests/test_health.py`.

---

## 9. `skyfield` 1.54 trips a NumPy 2.5 deprecation

**Decision.** Narrowly ignore that one warning in `pyproject.toml`; keep
`error::DeprecationWarning` for everything else.

**Why.** `find_events()` assigns to `ndarray.dtype`, which NumPy 2.5 deprecates.
Under strict `filterwarnings` this raised inside the per-satellite `try`, which
caught it and logged a warning — so pass prediction silently returned nothing
while the build reported success. The test suite caught it; production would
have shown an empty passes table with a green health panel.

Two things worth noting. First, this is **latent breakage**: when NumPy makes
it an error, pass prediction stops working. The ignore is scoped to that exact
message so the day skyfield fixes it, removing one line restores full strictness.
Second, it is a real demonstration of the risk in the broad `except Exception`
that resilience requires — it converts loud failures into quiet ones. That is
the right trade for one bad element set out of 142, but it means the tests are
the only thing standing between a systematic failure and a silently empty panel.

---

## 10. Geomagnetic severity gets its own vocabulary

**Decision.** The correlation banner reads *nominal / elevated / storm*, not the
health panel's *fresh / stale / failed*, though both use the same status colours.

**Why.** The first version reused the health badge, which rendered "FRESH —
Geomagnetic conditions nominal". Two unrelated severity scales sharing one
vocabulary on a single screen is the kind of ambiguity that makes an operations
dashboard untrustworthy. Sharing colour is fine — both are severity. Sharing
words is not.

Both badges pair a shape glyph with a text label, so state never depends on
colour alone.

---

## 11. Kp chart: height is the encoding, colour is redundant

**Decision.** Bar chart with a labelled G1 threshold line. Colour restates the
NOAA severity band but carries no information the chart would otherwise lack.

**Why.** Kp bands are an *ordered* scale, not a set of unrelated categories, so
the fills are a sequential ramp rather than a categorical palette. Adjacent
steps in a sequential ramp are supposed to resemble each other — that
resemblance is what makes the ramp read as ordered — which means hue can never
carry the distinction on its own. Measured on the shipped ramp, the worst
adjacent pair separates by **ΔE 8.1** unsimulated and as little as **0.7 under
protanopia** (OKLab ×100; measured with the palette validator, not estimated).
Two bands a protanope cannot tell apart is not a hypothetical.

So the ramp is stepped for **monotonic lightness** — quiet is the lightest step
and severe the darkest on the light theme, inverted on dark — on top of the
conventional green/amber/red hues. Severity therefore survives greyscale,
every form of colour blindness, and a bad projector, because it is encoded in
lightness as well as hue.

Bar height, the y-axis, and the labelled threshold line carry the actual
magnitude. Colour is reinforcement for the reader who already knows the bands.

The badge colours are a separate, darker ramp: they sit behind 0.75rem text, so
they are held to WCAG AA 4.5:1 against their own surface, which the chart fills
are not (fills sit behind no text).

The threshold label is left-anchored because the newest bars sit at the
right-hand end — and during a storm, when the threshold matters most, they are
tallest and collide with a right-anchored label.

---

## 12. Observer location is configurable by environment, not by UI

**Decision.** Default Rockville, MD (39.0840° N, 77.1528° W, 82 m). Override
via `ORBITAL_WATCH_LAT` / `_LON` / `_ELEV_M` / `_SITE_NAME`.

**Why.** The brief requires the location be configurable. It is a build-time
input, not a user preference: the site is pre-rendered, so a location picker
would mean either shipping every possible location's passes or adding the
client-side propagation the static-site architecture exists to avoid. An
environment variable lets the same code serve a different ground station by
changing one workflow setting.

Elevation of 82 m is Rockville's approximate ground elevation; the brief gives
only lat/lon, and 0 m would introduce a small but pointless bias in the
elevation mask.

---

## 13. Hosting: Cloudflare Pages, custom domain, committed seed snapshot

**Decision.** Cloudflare Pages, building from the repository, served at
`orbital.rodrigopimenta.com`. GitHub Actions no longer builds or deploys; it
only pings a deploy hook on a schedule.

**Why a custom domain.** `<user>.github.io/orbital-watch` and
`<project>.pages.dev` are both borrowed addresses — they break if the repo is
renamed, the account changes, or the host does. A domain I control is the only
stable identifier, which matters for a link that is meant to stay good for
years. The subdomain form also means one domain covers every future project
rather than one domain per project.

**What Cloudflare Pages changes, and the problem it created.** The build now
runs in an ephemeral container. There is no persistent `.cache/` between
builds, which is what the whole graceful-degradation story rested on: fetch
fails → fall back to the cache written by the previous run. On Cloudflare
every build starts cold, so a single upstream hiccup during a deploy would
have published an empty dashboard — precisely the failure this project exists
to demonstrate against.

**The fix: a committed seed snapshot** (`seed/`, ~108 KB), used only when
there is no cache *and* upstream is unreachable. The brief anticipated this
("implement the fetcher with a clearly-marked fixture fallback so the build
still succeeds"); the Cloudflare move is what made it necessary rather than
optional.

The part I care about is that it is honest **by construction**, not by
disclaimer:

- `seed/manifest.json` records each snapshot's true capture time, and that
  timestamp — not the build time — is what the health panel classifies. A
  months-old seed therefore ages into FAILED on its own, with no special case.
- A seed **never** reports FRESH regardless of how recently it was captured,
  for the same reason a cache fallback never does: it is served precisely
  because we reached nobody. Recency of the snapshot is beside the point.

The alternative was keeping GitHub Actions as the builder (it has
`actions/cache`) and pushing to Cloudflare with `wrangler pages deploy`. That
preserves the warm cache, but it needs a Cloudflare API token in CI, splits
the build across two providers, and makes the deploy path something you cannot
reproduce locally. Given the seed removes the reason to want the warm cache,
the simpler topology wins. The wrangler path is documented in the README as a
fallback.

**Secrets.** The deploy hook URL is a capability — anyone holding it can
trigger a build — so it lives in GitHub Actions secrets, never in the repo,
and the workflow writes curl output to a file rather than stdout so an error
body containing the URL cannot land in a public log. The workflow declares
`permissions: {}` and uses no third-party actions, because pinging a URL needs
neither.
