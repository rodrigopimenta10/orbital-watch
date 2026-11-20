# Decisions

Every open decision from `claude.md`, plus the ones forced by what the upstream
APIs actually did. Each entry records the choice, the reasoning, and what would
change my mind.

---

## 1. Frontend: plain HTML/CSS/JS, no build step

> **Still the decision; two details have changed.** The JavaScript is now
> three files, not one (§25): the page's logic was split from its drawing so
> it could be tested. And one of them, `config.js`, is rewritten when the
> site is assembled, so "what I wrote is what ships" holds for everything
> but those three lines.

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

> **Superseded twice.** First by §15 and §16 (2-hourly cadence; floor
> enforced by a snapshot refresher against a committed manifest), then by
> §22, where the floor is finally enforced where this section wanted it —
> in the fetcher — because there is now persistent state for the fetcher to
> enforce it against. Kept unedited as the record of the original
> reasoning. The *principle* (politeness enforced in code, not by the
> schedule) was right from the start; it took two redesigns to give it
> something to stand on.

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

> **Still true of every upstream call.** The dependency list has grown by
> one since: the Cloud Storage client, used for state and publication and
> nothing else (§28).

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

> **Unchanged in substance.** Read "build-time input" as "an input to the
> scheduled job": the variables are set on the Cloud Run job (or in the
> shell for a local run), and the document is computed for that one site.

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

> **Superseded by §20 and §27.** The site moved off Cloudflare Pages, and
> the seed snapshot this section introduced is gone: it existed because
> build containers kept nothing between runs, and the job now has real
> state. (Earlier, §14 had already corrected this section's trigger
> topology and §16 had promoted the snapshot to the primary Celestrak
> source.) The custom domain and the reasoning for it stand.

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

---

## 14. The refresh trigger moved to a Cloudflare Worker — §13's reasoning was wrong

> **Superseded by §20 and §26.** There is no Worker and no deploy hook any
> more. Two things from this section carried over: the principle that the
> refresh path must not hang off the code host's scheduler, and "watch the
> watchman", which is now Cloud Monitoring (§26). What this section got
> wrong is recorded in §19: it kept the snapshot refresher on GitHub
> Actions, reasoning that its failure mode was honest aging. It was — and
> the site spent hours stale because of it.

**Decision.** Rebuilds are triggered by a Cloudflare Worker Cron Trigger
(`infra/refresh-worker/`) POSTing the Pages deploy hook. The GitHub Actions
workflow that previously did this is deleted.

**The failure that forced it.** On 2026-08-06 GitHub Actions went into a
`major_outage`. The site's data went stale for over three hours and the
staleness banner fired — while Cloudflare, which hosts the site, was healthy
the entire time. Only the scheduler was down, and it took the whole freshness
story with it. Worse: that workflow had *never fired once* — the Actions API
showed zero runs since it was committed, so the deploy-hook path was entirely
unverified in production.

**Correcting §13, not quietly contradicting it.** §13 rejected building in
Actions partly to avoid "splitting the build across two providers." But the
topology it chose still spanned two providers — just for the *trigger* instead
of the build — and the seam is exactly where it broke. The principle was
right; §13 simply failed to apply it to the scheduler. Host and scheduler now
live on one platform, so a refresh can only stop for a reason that would have
taken the site down anyway.

**What stays on Actions, and why that is consistent.** `ci.yml` (tests belong
with the code host; CI going down does not affect the live site) and the
snapshot refresher (§16 — its failure mode is honest aging, not silent
freezing, and it needs repository write access that a Worker would need a
long-lived PAT to obtain).

**The Worker also watches the watchman.** The original failure was silent — the
only signal was a visitor noticing the banner. On every tick the Worker checks
the *previous* cycle: it fetches the deployed `health.json` and, if
`generated_at` is older than 3× the cadence, opens a GitHub issue (deduplicated
against an existing open one) or at minimum logs at error level. It also runs a
synthetic check on all eight public endpoints asserting status **and**
content-type — a 200 alone is worthless, because the Pages SPA fallback
answers 200 `text/html` for a missing asset, which is exactly how a blank page
once shipped while naive checks stayed green.

---

## 15. Cadence: every 2 hours, because hourly exceeded a hard limit

> **Superseded by §22.** The 500-builds-a-month cap no longer applies to
> anything: refreshing data is not a build. The cadence is ten minutes, and
> each source is asked at its own rate. The 3-hour space-weather window
> this section defended is now 30 minutes, derived from that cadence.

**Decision.** The Worker cron is `0 */2 * * *`.

**Why hourly was not sustainable.** Cloudflare Pages Free allows **500 builds
per month**, and every build counts — cron-triggered or push-triggered. Hourly
is 720/month: the allowance would run out around day 20, after which rebuilds
simply stop and the site silently freezes with no failure anywhere to look at.

| Cadence | Builds/month | Headroom vs 500 |
|---|---|---|
| Hourly | 720 | −220 — over the cap |
| Every 90 min | 480 | 20 — too tight to absorb pushes |
| **Every 2 hours** | **360** | **140** |
| Every 3 hours | 240 | wastefully conservative |

Snapshot-refresh commits (§16) carry `[CI Skip]`, which Pages recognises, so
they cost zero builds — the budget stays 360 + development pushes.

**Two hours is independently right, not just affordable.** Celestrak
regenerates GP data every 2 hours, so polling faster returned data that did
not exist yet; the slower cadence also halves pressure on their rate limiter;
and it stays inside SWPC's 3-hour freshness window, so a normal run reports
FRESH.

**Accepted tradeoff.** At this cadence, *one* missed run puts space weather at
4 hours old and the site reports STALE. That is correct behaviour, not a
regression — the page is telling the truth about its own data.
`SPACE_WEATHER_STALENESS.fresh_within` stays at 3 hours because Kp is a
3-hourly index; loosening a threshold to suppress an honest signal would
destroy the one thing this project is for.

---

## 16. Builds read Celestrak from a committed snapshot; one refresher owns fetching

> **Superseded by §21 and §22.** State moved out of git into a bucket, and
> the one job that fetches is also the one that publishes. The two bugs
> recorded below are still guarded against, in their new form: a
> "not modified" answer advances the confirmation time (and the panel now
> also says how old the download is), and a failed fetch never counts as
> contact.

**Decision.** §10.3 Option A. The build never contacts Celestrak: it reads
`seed/`, which a scheduled job (`refresh-snapshot.yml`, every 6 hours)
refreshes and commits. The new `SNAPSHOT` outcome is classified purely by
age — the intended source, not a fallback.

**Why the old design's politeness guarantee was fictional.** The 6-hour
refetch floor was enforced against the on-disk cache, and Cloudflare Pages
build containers are ephemeral: cold cache on every build, so the floor never
applied in production and every build hit Celestrak three times. Celestrak
throttled intermittently — one observed build had all three groups time out at
20s and fall back to the seed while SWPC succeeded; the build ten minutes
later got everything live. §3's "the schedule, not just the code, has to stay
conservative" half-acknowledged the problem without resolving it. The floor is
now enforced by the refresher against the manifest timestamp — the only state
that actually persists between runs — which makes it real for the first time.

**What it buys beyond politeness.** Builds are deterministic and reproducible
offline, and the committed snapshots double as a coarse TLE time series.

**Two bugs the implementation surfaced, both worth recording:**

1. **NOT_MODIFIED must advance the confirmation time.** Celestrak answers
   "not updated since your last download" when we already hold its newest
   element sets. Recording the payload's original download time would age a
   snapshot upstream had *just confirmed current* — STALE while holding the
   freshest data in existence. The manifest records when currency was last
   confirmed, which is the question the health panel is actually asking.
   Per-satellite element age is reported separately from the EPOCH field, so
   this hides nothing.
2. **A failed refresh must not count as a refresh.** The fetch helper falls
   back to cache and then seed — and the seed *is* the snapshot. Without a
   guard, a refresher that could never reach Celestrak again would read its
   own output, write it back, advance the timestamp, and report a fresh
   snapshot forever: precisely the species of dishonesty this project exists
   to argue against. Only `LIVE` or `NOT_MODIFIED` outcomes update the
   manifest.

**Why the refresher runs on GitHub Actions after §14 moved the trigger off
it.** The rule from §14 is about failure modes, not providers. If the rebuild
trigger dies, the site freezes *silently* — so it lives with the host. If the
snapshot refresher dies, the snapshot ages and the health panel reports STALE
then FAILED with its true capture time, while rebuilds keep publishing —
honest degradation, observed by both the dead-man check and a CI check that
fails when the manifest is older than 30 days. And a scheduled commit needs
repository write access, which Actions has natively.

---

## 17. Two production bugs from 2026-08-06, recorded so they stay fixed

> **Both still guarded.** The negative-age clamp and its regression tests
> carried over. The seed test in the second entry no longer exists (§27),
> but its lesson was applied more widely in the rebuild: every test that
> propagates the fixture element sets now pins its clock to the time they
> were captured.

**Negative data age.** Threading the build's *start* time into health
classification made sources fetched during the build resolve to a negative
age, which rendered as "Fetched from upstream in the future ago" on the live
site. Two fixes, deliberately layered: `run_build` passes the caller's
explicit clock (or wall-clock), and `health` clamps age at zero regardless —
belt and braces, because this string ships to the public page. Regression
tests pin both.

**A test that rots with the fixture it reads.** The cold-build seed test
asserted seed-specific wording in the health detail — but age is evaluated
before outcome in classification, so once the committed seed aged past a
source's freshness window, the age wording fired instead and the test failed
permanently. Against a live clock it passed only for the few hours after each
re-seed. The clock is now derived from `seed/manifest.json` (specifically the
SWPC entries, since the Celestrak entries are refreshed on their own schedule
and would skew the pinned instant). A test that fails on the age of a
checked-in fixture rather than the behaviour under test is a trap for whoever
touches the repo next — the failure looks exactly like a real regression.

---

## 18. The fourth production bug: two correct components composed into a broken system

> **The fix below was correct and the bug came back anyway** — see §19.
> The floor derived here assumed the cron would fire roughly on time. The
> derivation, its simulation and its tests are deleted (§22): with one
> scheduler and stored state there is no second schedule for the floor to
> compose with.

**Observed, 2026-08-07, on the live site.** All three Celestrak sources STALE
at 9.5h old while every SWPC source was fresh — and *nothing had failed*. The
refresher's 14:30 UTC run executed, succeeded in 18 seconds, and committed
nothing, because at that moment the snapshot was 5h57m old: three minutes
under the 6-hour politeness floor. It correctly skipped. The next opportunity
was six hours later, by which point the snapshot was ~12h old against an
8-hour freshness window.

**The mechanism.** The cron interval equalled the floor. Each capture lands
minutes after a tick (fetch and commit take time), so the tick arriving at
floor-age is always those same minutes too early — it skips, deterministically,
every cycle, and the effective refresh interval becomes twice the floor. Not
a race: a limit cycle. The floor was tested. The cron was tested. The bug
lives only in their composition, which no test covered.

**The diagnostic compounded it.** The health detail read "the scheduled
refresher has likely stopped committing" — accusing the right component of
the wrong failure. It had run and correctly declined. The message now
distinguishes the two cases the build can actually tell apart from age alone:
within `floor + cadence` is expected scheduling, beyond it means cycles are
genuinely being missed.

**The fix, and an honest note about the spec.** The §11 write-up prescribed
"cron every 2h, keep the 6h floor," projecting refreshes landing at 6–8h.
The regression simulation it *also* prescribed showed that projection wrong:
with the capture-after-tick offset, the steady state converges to a gap of
exactly 8h0m — the knife-edge of the window, tipped over by any of the
routine minutes-late cron ticks GitHub Actions delivers. The spec's suggested
parameters failed the spec's own invariant by the width of one run's
duration.

So the floor is now **derived, not chosen**:

```
floor = fresh_within − cadence − scheduling_margin
      = 8h − 2h − 30m = 5h30m
```

Steady-state refreshes land at ~6h gaps with two hours of headroom, Celestrak
still sees at most ~4–5 requests per group per day (its own data only changes
every 2h), and `test_scheduling.py` enforces the derivation three ways: the
inequality itself, a discrete-event simulation of cron-plus-floor that must
stay inside the window (and provably reproduces the old bug when fed the old
numbers), and a parser that pins the workflow's actual cron string to the
cadence constant so the YAML and the code cannot drift apart silently.

**The general lesson, worth stating because it recurs:** unit-testing both
sides of a boundary proves nothing about the boundary. Every prior bug in
this file was a component wrong in isolation; this one had no wrong
component. The properties worth the most tests are the ones that only exist
between pieces — which is also why the fix is an invariant over three
constants rather than new behaviour in any one of them.

---

# The rebuild (November 2026)

Sections 19 to 29 record the replacement of the delivery architecture. The
core of the project did not change: SGP4 propagation checked against
published vectors, the health state machine, one never-raising fetch
function. Everything about scheduling, state and delivery did. The sections
above are left as they were written, each with a note saying what became of
it, because the path here is the point of this file.

---

## 19. The fifth production bug: a design that needed a scheduler to be punctual

**Observed, 2026-11-20, on the live site.** All three Celestrak sources
STALE: element sets 15 hours old against an 8-hour freshness window. As in
§18, *nothing had failed*.

**The mechanism.** After §18 the snapshot refresher's cron was
`30 1-23/2 * * *` — every two hours, twelve runs a day — with a 5 h 30 m
politeness floor derived so that some tick would always land between the
floor clearing and the window closing. GitHub Actions ran it about three
times a day. The run start times, UTC:

| 18 Nov | 19 Nov | 20 Nov |
|---|---|---|
| 04:39, 14:27, 22:40 | 04:25, 14:38, 22:44 | 04:11, 14:16 |

The 04:11 run on the 20th arrived 5 h 27 m after the previous capture, three
minutes under the floor, and correctly skipped. The next run came ten hours
later.

**Why the §18 fix did not hold.** §18 treated the bug as an arithmetic error
between three constants and fixed the arithmetic. The derivation
`floor = fresh_within − cadence − margin` is sound *if ticks arrive roughly
every `cadence`*. They arrived roughly every eight hours. No value of the
margin survives a scheduler that delivers a quarter of its ticks. GitHub
describes scheduled workflows as best-effort, and §18 allowed for that with a
thirty-minute margin; what it got was gaps of six to ten hours.

**The second time, not the first.** §14 recorded an outage caused by
depending on GitHub Actions' scheduler, moved the *rebuild* trigger off it,
and then kept the *refresher* on it, arguing that its failure mode was
"honest aging, not silent freezing". That argument was true and beside the
point: the site was honestly, visibly stale for hours every day. An honest
report of an avoidable degradation is still an avoidable degradation.

**Why the class of bug kept returning.** Each of the five bugs was fixed
correctly, and each fix left the structure that produced it:

1. *There was no state.* Build containers kept nothing between runs, so the
   only memory the system had was commits to its own repository. That made
   the fetcher need write access to git, made every refresh a commit, and
   made the politeness floor something only a separate job could enforce.
2. *Refreshing data meant redeploying the site.* A cap on builds (§15) then
   set the cadence for everything, including data that changes by the
   minute.
3. *Three schedulers on two platforms had to line up*: a Worker cron for
   rebuilds, an Actions cron for the snapshot, and Pages' own build queue.
   Each could be late on its own, and the freshness window was tuned against
   their sum.
4. *The thresholds were hand-tuned against the schedules.* Floor, cadence
   and window were three numbers that had to satisfy an inequality, guarded
   by a test. A guard on an inequality does not help when one of its terms
   is not under your control.

**The decision.** Stop repairing instances. Give the system state, one
scheduler, and a delivery path that does not involve a deploy (§20–§26). The
measure of the new design is not that this bug cannot recur in this form,
but that the things it was made of are gone: there is no second schedule, no
floor-versus-cron relation, and no state in git.

**What would change my mind.** Nothing would send me back to state-in-git.
If Cloud Scheduler proved unreliable at this cadence I would want the alert
in §26 to tell me, and I would add a second trigger for the same job rather
than a second job — the job is safe to start twice (§21).

---

## 20. One scheduled job on Google Cloud replaces builds, hooks and commits

**Decision.** A container run as a **Cloud Run job**, started by **Cloud
Scheduler** every ten minutes. Each run fetches what is due, propagates,
evaluates health and publishes one document to a public **Cloud Storage**
bucket. The page is static files on **Firebase Hosting** and reads that
document. Project `portfolio-prod-0p1ht`, region `us-east4`, all of it in
Terraform (`infra/`).

**This supersedes §13, §14, §15 and §16**, and replaces a constraint in the
original brief ("no runtime backend", Cloudflare Pages); `claude.md` §12
records the owner's decision to replace it.

**What "no backend" was protecting, and whether it still is.** The original
constraint existed for three reasons: an upstream outage must not take the
site down, hosting must be cheap and unable to fall over, and "last updated"
must be a measured fact. All three still hold, and the first two for the
same reason as before: a visitor's request is answered by a CDN or a bucket,
and never causes code to run or an upstream to be called. What changed is
that the *producer* of the static data is a scheduled container instead of a
build. Nothing listens for requests. Between runs nothing is running.

**Why a Cloud Run job and not something else.**

- *A Cloud Function or a Cloud Run service triggered over HTTP* would work,
  but each is a request handler pretending to be a batch job: it needs an
  endpoint, an invoker policy, and care about request timeouts. A job is the
  thing this is — run to completion, exit code, done.
- *Keeping the build, adding a bucket for state* (the "Option B" of the old
  §10.3) fixes the state problem and none of the others: data refresh would
  still be a site deploy against a build cap, on two platforms.
- *A VM with cron* is the simplest thing that has state and a scheduler. It
  also runs around the clock to do a few seconds of work every ten minutes,
  and it is a machine to patch.

**Why Google Cloud.** The portfolio site already runs there, in this
project, with Terraform, keyless deploys and a budget alert in place. One
platform for scheduler, compute, state and hosting means one place where a
failure can be, one IAM model, and one bill. §14 argued for exactly that and
then stopped half way.

**The deploy path, and what it deliberately does not do.** A deploy changes
which code runs. It does not refresh data, and data does not need a deploy:
if this repository is never touched again, Scheduler still runs the job
every ten minutes. There is no cron in the repository, and a test fails if a
scheduled workflow is added, because a second scheduler is how this broke.

**Cost.** About a dollar a month at list prices, much of it inside free
allowances (`infra/README.md`). The old design cost nothing. I am paying
roughly a dollar a month for state, a scheduler with an alert on it, and
ten-minute data; that is the right price.

---

## 21. State lives in a bucket, behind a small interface, with conditional writes

**Decision.** Each source has one object in a private bucket: its last good
payload together with when it was downloaded, when upstream last confirmed
it, when the last attempt was and how it ended, and the validators upstream
sent. Code reaches storage through a three-operation interface
(`store.py`): describe, read, conditional write. Two implementations — a
local directory and a Cloud Storage bucket.

**Why one object per source, payload and metadata together.** The first
design kept a payload file and a `.meta` file side by side and had code for
the cases where they disagreed. One object cannot disagree with itself.
Per-source rather than one big object so that damage, or a lost write race,
costs one source.

**Why the interface, and why the local implementation is not a mock.**
Tests, CI, the container's smoke test and a laptop all run the real job
against a directory. The local store honours the full contract, including
conditional writes, so the code paths the tests exercise are the ones
production runs; the only thing not exercised offline is the translation to
the Cloud Storage client, which is tested against a fake client.

**Why conditional writes.** Every object has a generation. A write that
names the generation it read is refused if the object has changed since. The
job is normally alone — one task, one schedule — but a manual run during a
scheduled one, or a scheduler retry after a lost response, can overlap it.
Without the precondition the slower run would silently replace newer state
with older. With it, the slower run is told, keeps the other run's record,
and says so in the health panel.

**Failing closed.** If the state store cannot be *read*, a source with a
politeness floor is not fetched at all. "No record of the last request" must
not be read as "free to ask": a storage outage would otherwise turn into a
burst of requests at a server that rate-limits. Sources without a floor are
still fetched, so the page keeps live space weather through a state outage.

**Damage.** A state object that is not JSON, or the wrong shape, or carries
a payload the current code would reject, degrades to "nothing stored" and is
replaced on the next successful fetch. Eight shapes of damage are tested.
State outlives the code that wrote it; reading it back unvalidated is how a
fallback turns into a crash.

**Versioning.** The state bucket keeps overwritten versions for a week, so a
state object ruined by a bad release can be read back. Soft delete is turned
off on both buckets: it would be a second, billed copy of the same history.

---

## 22. Each source is asked at its own rate; the thresholds follow from the schedule

**Decision.** The job runs every ten minutes (`REFRESH_CADENCE`). What it
does on a given run depends on the source:

| Source | Asked | Why |
|---|---|---|
| NOAA SWPC (four products) | every run, conditionally | Small, change by the minute, published for polling. `If-None-Match` / `If-Modified-Since` make an unchanged product cost a header exchange |
| Celestrak (three groups) | at most once per 2 h each | Celestrak regenerates GP data about every two hours and says so in its own reply. Asking sooner cannot return newer data, and it rate-limits those who try |
| Celestrak, after a failed attempt | not again for 30 min | A struggling or throttling server should not be retried on every run |

The floor is enforced inside the fetch function against the stored record of
the last attempt. It holds for any way the job can be started.

**This supersedes §3, §15 and the derivation in §18.**

**The thresholds are now expressions, not numbers.** In `config.py`:

```
PIPELINE_SILENCE_LIMIT        = 3 × REFRESH_CADENCE                  (30 min)
SPACE_WEATHER  fresh_within   = PIPELINE_SILENCE_LIMIT               (30 min)
               stale_within   = one Kp interval                      (3 h)
TLE            fresh_within   = Celestrak floor + SILENCE_LIMIT      (2 h 30 m)
               stale_within   = 24 × Celestrak's publication interval (48 h)
EPHEMERIS_WINDOW              = PIPELINE_SILENCE_LIMIT               (30 min)
```

"Fresh" means the same thing for every source: *not more than three runs
overdue*. Space weather is due every run, so it is fresh for three cadences.
A Celestrak group is due every two hours, so it is fresh for two hours plus
three cadences. Change the cadence and the windows move with it.

In §18 the relation between floor, cadence and window was an invariant that a
test defended. Here it is a definition, and there is nothing to defend: the
window is *made of* the floor. The scheduling simulation, the cron parser and
`test_scheduling.py` are deleted rather than left to guard a relation that no
longer has independent terms. One judgement remains and is labelled as one:
48 hours as the point where element sets stop being called current.

**One cadence constant across two languages.** The cadence is a Terraform
variable. It sets the cron expression, is passed to the job as an environment
variable, and sizes the "no successful run" alert. The code has a default
for local runs, and a test pins that default to Terraform's.

**A subtlety worth recording: failure has to persist between retries.** A
Celestrak fetch fails; the next two runs do not retry it (not due). If those
runs reported "deliberately not contacted — fresh", the panel would
alternate stale, fresh, fresh, stale while nothing had recovered. The stored
record carries the last outcome, and a source that is waiting to retry keeps
reporting the failure it is waiting on.

**The old windows were wider** (8 h and 3 h fresh). They were wide because
the delivery path was slow and unreliable, and the window had to cover it.
Tightening them is not a change of principle; it is what the principle
always implied once the path allowed it.

---

## 23. One document, validated, then published in one write

**Decision.** The job publishes a single object, `latest.json`, containing
what used to be five files (meta, sky, passes, space weather, health). It is
validated before it is written. It is served with
`Cache-Control: public, max-age=60` and CORS limited to the site's origins.

**Why one object.** One object write is atomic; five are not. With five
files a reader could fetch this run's passes beside the last run's health —
and for a project whose claim is that the health panel describes the data
next to it, that mixture is the worst thing it could show.

**Why validate, and what against.** Two things. *Shape*: every field the page
reads is present and of the type it expects, tracks have the declared number
of samples, nothing is NaN. *Honesty*: the rules in the README's failure
table are checked again on the finished document, by code that shares
nothing with the code meant to uphold them. No source is fresh on a fallback
outcome. No source is fresh without a date. Every source is accounted for.
The overall state is the worst one. A later change that breaks
classification cannot publish its mistake.

**What happens to a document that fails.** It is not published. The previous
document stays where it is — still true, only older, and the page already
tells readers how old. The run exits non-zero, which alerts the owner.
Publishing something malformed is strictly worse than publishing nothing.

**A stage that crashes does not drop its sources.** If our own code throws
while interpreting space weather, the document reports those four sources as
failed with the reason and the satellite panels are unaffected. Before this,
a crashed stage would have produced a health panel with rows missing, which
the validator would rightly refuse — taking the working panels down with the
broken one.

**An older run cannot replace a newer document.** Publication compares the
generation time of what is already published, then writes conditionally on
it not having changed. A run that finds something newer stands down and
exits 0: what readers see is truthful and more recent than what it had.

**Why 60 seconds of caching and not zero.** The page computes every age from
timestamps inside the document, so a cached copy is never *misreported* —
it is shown as being as old as it is. Sixty seconds keeps a burst of
visitors from each costing a bucket read, at the price of readers being up
to about two minutes behind (cache plus poll interval).

---

## 24. The sky is shown as it is when the page is read

**Decision.** For each object that comes within two degrees of the horizon
in the next thirty minutes, the document carries its position relative to
the ground station every thirty seconds, as an east/north/up vector in
kilometres plus altitude. The page interpolates linearly along that track to
its own clock, every five seconds. When the track has run out, the sky table
is replaced by a statement that positions are no longer current.

**The problem.** The first design computed positions once per build and the
page displayed them under the heading "Sky view" with a footnote saying they
were a snapshot. A low-orbit satellite crosses the sky in about ten minutes.
A table of where things were up to two hours ago is not a sky view.

**Why not propagate in the browser.** It would be exact, and it would mean a
second SGP4 implementation, in JavaScript, verified by nobody. The Python
one is checked against published vectors (§2). The brief for the rebuild was
explicit and I agree with it: no orbital mechanics in the browser.

**Why vectors and not angles.** The obvious track is elevation and azimuth
per sample. Interpolating those is badly wrong exactly where it matters: on a
pass through the zenith the azimuth swings through half a turn in seconds
and the elevation peaks between two samples, so a straight line between
samples cuts the top off the pass. In space, though, a satellite's path over
thirty seconds is very nearly a straight line — it moves about 230 km along
an arc whose sagitta is about a kilometre. So the track is the *vector*, the
page interpolates that, and converts to elevation, azimuth and range with
two arctangents and a square root. That is coordinate conversion, not
orbital mechanics: no propagation, no reference frames, no time systems.

**Held to SGP4, in both languages.** A Python test compares the
interpolated track with SGP4 at the instants halfway between samples, where
the error is largest, across a pass that goes high overhead: under 0.2° and
2 km (measured: about 0.08° and 0.8 km). The same check runs on the
JavaScript, against reference values *written by the Python pipeline* into a
committed fixture — a real track for a pass peaking near 84° — so the page's
arithmetic is checked against SGP4 and not against numbers typed into a
test. A third test fails if that fixture drifts from what the pipeline
produces.

**Why thirty minutes.** The track has to outlast the wait for the next
document, comfortably. It is set equal to the silence limit (§22) on
purpose: the moment the track runs out is the moment the page starts saying
the pipeline is silent. Positions are therefore never shown as current after
the page has stopped believing the pipeline is alive.

**Cost.** The document is about 100 KB, most of it tracks.

---

## 25. The page loads live, falls back to a baked copy, and watches the pipeline itself

**Decision.** The page tries the live document in the public bucket, with a
four-second timeout. If that fails for any reason it loads a copy baked into
the site at deploy time. If that fails too, each panel shows an error with
both reasons. It says which copy it is showing. It checks for a newer
document once a minute without reloading.

**A fallback never claims freshness — applied to the page itself.** §8
established that a source served from stored data after a failed fetch is
stale however young the data. The baked copy is the same situation one level
up: the page has lost contact with the pipeline. So when it is showing the
baked copy, nothing in it is reported fresh, and the health panel says why
its states differ from what the document recorded. I considered letting a
recently baked copy keep its states; that would be the page vouching for
data it could not check.

**The page only moves forward.** A single failed poll does not swap a live
document for an older baked one. The page keeps what it has and records that
the check failed, in the line that says what it is showing.

**The page is the visitor's dead-man's switch.** It computes the document's
age against the reader's clock, and the limit from the schedule the document
itself carries. Once the document is older than three runs it says, at the
top, when the pipeline last published. Nothing the pipeline does or fails to
do can prevent that, which is the property the first design's Worker-based
check did not have: it ran on the same platform as the thing it watched.

**Why `core.js` exists.** §1 chose one hand-written script with no build
step, and that still holds — there is still no bundler, no `node_modules`,
no `package.json`. But the page now makes decisions that matter (which copy,
how stale, where a satellite is), and decisions want tests. They are in a
file with no DOM in it, loaded by a plain `<script>` tag in the browser and
by `require` under Node's built-in test runner. 45 tests, no dependencies.

**`config.js` is the one generated file.** Where the live document lives
differs between a laptop and production and is not known to `web/`. The
committed file describes a local run; the deploy writes one naming the
bucket. It is a file, not an inline block, so the Content-Security-Policy
stays `script-src 'self'`; `connect-src` gained exactly one origin,
`https://storage.googleapis.com`, and a test spells the whole policy out.

**Redrawing without disturbing the reader.** A new document arrives every
ten minutes while someone may be reading. The redraw remembers which
`<details>` were open and how far each table was scrolled, and restores
them. The five-second tick touches only text that depends on the clock,
except the sky table, whose rows are replaced inside a scroll container that
is kept.

---

## 26. Something independent watches, and a failed run means one thing

**Decision.** Four alerts in Cloud Monitoring, in Terraform, to one email
channel whose address is a variable with no default:

1. an execution of the job failed;
2. no execution has succeeded for 30 minutes (metric *absence*);
3. the published document is unreachable, not public, or not JSON whose
   `kind` is `orbital-watch.latest`;
4. the site is unreachable or is not serving the page (matched by title).

**The exit code is an interface.** The job exits non-zero only when it
could not publish: storage refused the write (1), our code has a bug (2), or
the document failed validation and was withheld (3). **An upstream outage is
not a failed run.** The document says the source is down, the page shows it,
and the job exits 0, because reporting an outage truthfully is the job
working. If upstream trouble failed the run, alert 1 would fire every time
NOAA had a bad hour and would stop meaning "the pipeline is broken".

**No retries on the job, exactly two on the start call.** A run that failed
should reach the alert; the next scheduled run, minutes away, is the retry.
Cloud Scheduler does retry the *call that starts the job* if the Cloud Run
API refuses it — twice, 30 and 60 seconds apart, over before the next tick.
(Its `max_retry_duration` is set to zero, meaning unlimited, on purpose:
Scheduler keeps retrying until both the count and the duration are used up,
so any non-zero duration would turn "two" into "at least two".)

**Why absence needs its own alert.** A threshold on failed executions says
nothing when the job is not being started at all — there is no count to
compare. That is the failure this project has actually had, twice.

**Why the checks match content.** §14 recorded a blank page shipping while
naive checks stayed green, because a 200 with the wrong body is still a 200.
The document carries a `kind` field for the check to look for.

**What these alerts cannot see**, and what covers it. They do not know
whether the *data* is fresh — a job that runs and publishes "all seven
sources failed" every ten minutes is, to Monitoring, healthy. That is
correct: it is healthy, and the page says the sources are down. An alert on
upstream health would be an alert about someone else's service.

---

## 27. The seed snapshot is gone

**Decision.** `seed/` and the `seed` and `snapshot` outcomes are deleted. On
a first-ever run with empty state and no network, the job publishes a
document in which every source is failed, and the page renders its
per-panel empty states.

**Why it existed.** §13: build containers started cold every time, so one
upstream hiccup during a deploy would have published an empty dashboard.
The seed made a cold start show real, old satellites instead.

**Why it is not needed.** A cold start now happens once — the first run
after the state bucket is created — instead of on every build. After that
the job always has its own last good data, which is newer than any committed
seed could be. Keeping a snapshot in the repository for that single run
would mean keeping the refresh-and-commit machinery, or shipping a seed that
is months old and saying so. The honest empty state costs less than either.

**What replaces it for readers.** The copy of the document baked into the
site at deploy time is the page's fallback (§25), and it keeps the seed's
rule: it is never presented as fresh.

**What I gave up.** A deploy to a brand-new project while Celestrak is down
shows an empty sky until Celestrak answers. The page says why.

---

## 28. Dependencies: the Cloud Storage client is the one addition

**Runtime dependencies are now three:** `skyfield` (§2), `sgp4` (imported
directly by `propagate.py`), and `google-cloud-storage`.

**Why not stdlib `urllib` here too, as §7 chose for upstream calls.** I
considered writing the two calls needed (read an object with its generation;
upload with a generation precondition) against the JSON API by hand, with a
token from the metadata server. It is perhaps 150 lines and would keep the
dependency list at two. I chose the official client because of what those
lines would be responsible for: obtaining and refreshing credentials in two
environments (the job's service account on Cloud Run, a developer's on a
laptop), a multipart upload, and retry behaviour that is only safe because
the write is conditional. §7's argument was that seven GETs sit squarely
inside what `urllib` does well. Authenticated conditional writes do not, and
this is the code that publishes — the one place where being subtly wrong
breaks everything else.

**How the cost is contained.** The client is imported in `store.py` and
nowhere else, lazily, behind the interface in §21. Every test, the CI
no-network run and local development use the directory implementation and
never construct a client. The test suite's network guard was extended from
`urlopen` to sockets, because this library does not go through `urlopen`.

**What it does cost.** About twenty more packages in the lockfile and a
larger image (about 350 MB uncompressed). On a job that runs for seconds every ten
minutes, image size affects start time slightly and nothing else.

**Development dependencies** are unchanged: `pytest`, `ruff`, and `pyyaml`,
which a test uses to read the workflow files. The frontend has none: its
tests use Node's built-in runner.

---

## 29. "Not updated since your last download" — whose download?

**Decision.** Celestrak's "not updated" reply (§6) is treated as confirmation
that our stored element sets are current **only if the download time it
quotes is not later than our own**. Otherwise the group is reported stale:
upstream is withholding data and what we hold cannot be called confirmed.

**Why this came up now.** The reply reads "GP data has not updated since your
last successful download of GROUP=… at *time*". Celestrak keeps that per
requesting address. The first design ran from GitHub's runners and already
noted that shared addresses could trigger the reply on a first request. The
new one runs from Cloud Run, whose outbound addresses are shared between
customers. If another tenant on the same address downloaded a group an hour
after we did, Celestrak will tell *us* nothing has changed since *their*
download — and the data may well have been regenerated between ours and
theirs.

**Why it matters.** §16 established that a "not modified" answer advances
the confirmation time: upstream has just told us our copy is current. That
is only true if the answer is about our copy. Advancing the time on someone
else's download would report a group as freshly confirmed while holding
element sets up to two hours behind what upstream has — a small error, and
exactly the kind this project exists to refuse: a claim of contact we did
not have.

**The rule.** Quoted time at or before ours (within five minutes, for the
difference between their clock at serving and ours after validating):
nothing has changed since a moment no later than our download, so we are
current. Quoted time later than ours: withheld, stale, retried in thirty
minutes. Quoted time unreadable: also withheld — an answer we cannot
interpret is not evidence.

**The limit of this.** It makes the report honest; it does not get us the
data. If a neighbour on a shared address reliably fetched each update before
we did, the group would stay stale and the panel would say why. The remedy
would be a reserved outbound address (a NAT gateway), which costs more than
the rest of the system put together, so it waits for evidence that the
problem is real. I could not test this against Celestrak itself: on the day
of the rebuild its whole site was answering HTTP 503, which the new job
reported, correctly, as three failed sources and a successful run.

---

## 30. An independent review before the first deploy found seven defects, and more

**What happened.** Before the rebuild was deployed, the branch was reviewed
independently of its author, and the job was run once against the real
upstreams. The review confirmed six defects by reproducing them with fakes,
and CI found a seventh. It also raised six suspicions: four were confirmed,
one was fixed without being confirmed, one was left. None of this had been
caught by 308 passing tests, because each lived in a case the tests had not
thought to construct. Everything fixed was fixed before anything shipped,
and each fix has a test that failed first.

**Confirmed, with the cause of each in a line:**

| # | Defect | Cause |
|---|---|---|
| 1 | A throttled body was read for minutes; past 150 s the pass list was published empty, past 300 s the task was killed and nothing was | The "hard timeout" was a socket timeout. It bounds each read, not the response, and a slow steady body never trips it. Seen live the same day: Celestrak sent 5 MB at about 8 KB/s |
| 2 | One `NaN` or `Infinity` from upstream withheld the whole document | Python's JSON parser accepts both, and turns `1e999` into infinity. The value was stored, and failed validation on every later run |
| 3 | With runs overlapping, a failure record displaced a fresh payload | The conditional write treated every lost race alike, and gave up even when the loser held the newer data |
| 4 | With state readable but not writable, every run asked Celestrak | The floor was checked against a record written *after* the request. A run killed mid-request left none either |
| 5 | A slow reader clock made stale look fresh, hid a silent pipeline, and showed expired positions as current | Every age on the page was measured against the device clock and nothing else |
| 6 | A live poll could move the page to an older document | "Live" was taken to mean "newest"; a cache can answer with an older copy |
| 7 | The page's tests failed in CI | `node --test <directory>` searches the directory on recent Node and treats it as one file on Node 22, which the runner had. Never run on the version CI used |

**Suspected and confirmed:** a publish whose answer was lost was reported as
a failed run although the document was up; a published document dated in
the future made every later run stand down and exit 0 without publishing;
a truncated Starlink list still filled the sample of sixty and passed the
collapse check, which compared samples; the assessment banner went on
saying "storm in progress" after the Kp source behind it had failed by age.

**Suspected, not confirmed, fixed anyway.** The published document is public
and cacheable, so Cloud Storage may answer even the job's own read of it
from a cache, with a stale generation; the conditional publish would then
fail twice and the run exit 1 although a newer document was up. I could not
establish from here whether that cache applies to authenticated reads. The
fix — ask for the live generation, then read that generation by number —
costs one metadata call and is right in either case.

**Suspected and left.** On a change of schema version, a browser holding the
previous `core.js` (assets may be cached for an hour) refuses both the live
document and the baked copy, and shows the error state until its cache
expires. It is real, it is confined to the hour after a schema bump, and the
proper fix is fingerprinted asset names — a build step, which §1 decided
against. A tab that is already open is unaffected: it keeps the document it
has and says the newer one could not be read. If the schema ever does
change, the page should first be taught to read both versions, and the
pipeline switched over only after that page has been out for an hour.

**What the fixes have in common.** Five of the seven were the same mistake
in different places: *a guarantee stated more strongly than it was
implemented*. "Hard timeout" meant a per-read timeout. "The floor holds
however the job is started" meant however it is started, provided it also
finishes and can write. "Recomputed against the reader's clock" assumed the
clock. In each case the fix was to make the mechanism match the sentence —
a deadline over the whole response, a record written before the act it
records, a clock with a floor under it — rather than to soften the sentence.

**What I take from it.** The tests I wrote covered the failures I had
imagined, and I had imagined upstreams that fail *cleanly*: refuse, time
out, return garbage. The live run failed slowly and partially, which is how
real services fail. A reviewer who starts from "how could this claim be
false?" finds different things from an author who starts from "does this
work?", and a system whose whole claim is honesty about failure needs the
first kind of reading before it is trusted with the second.

---

## 31. Starlink comes from a 90 KB group, not a 5 MB one

**Decision.** The Starlink objects on the page are taken from Celestrak's
`last-30-days` group (everything launched in the last thirty days, about 200
objects) filtered to names beginning `STARLINK`, and no longer from the full
`starlink` group. The sample is still sixty, still the most recent epochs
(§4). The group is labelled "Recent Starlink", and the page's coverage line
counts against what matched ("60 of 107").

**Why.** The full group is about 11,000 objects and 5 MB. Downloading it
twelve times a day to show sixty satellites was about 60 MB a day. Celestrak's
own documentation says what that costs them: they have a monthly bandwidth
budget they are exceeding, they ask users to consider how much data they
need, and an address using more than roughly 100 MB a day can expect to be
firewalled. This job's address is shared with other Cloud Run tenants, so
its own 60 MB was most of that allowance before anyone else's traffic was
counted. The new request is about 90 KB, so the three groups together are
now about 1.5 MB a day.

It is also the fragile request. On the day of the move, with Celestrak
struggling, the two small groups got through from Google Cloud and the 5 MB
one never did. The smaller the request, the more often it completes on a bad
day; and a download abandoned part-way still counts, on Celestrak's side, as
the one download allowed until the data next changes (§29).

**What it costs.** The sixty are no longer drawn from the whole
constellation, only from recent launches, which are often still raising
their orbits. For a page whose purpose is representative visibility over one
ground station that is an acceptable sample, and the label says what it is.
If thirty days pass with no Starlink launch, the filter matches nothing; that
is treated as a refusal to refresh, so the stored group is kept and ages
honestly (there is a test for it).

**Recorded shape.** The real response of 2026-11-20 is a fixture
(`tests/fixtures/celestrak_last_30_days.json`: 204 objects, 107 Starlink).

---

## 32. The move to Google Cloud, 20 November 2026: what was carried over

The job's first runs on Google Cloud (16:10 and 16:13 UTC) found Celestrak
answering HTTP 503 to every request; it was failing for everyone that
afternoon, not for this address in particular. The state store was empty, so
the page had space weather and no satellites, and said so.

**What was done.** The stations and weather groups had been fetched
successfully at 15:25 UTC by this same code, run once from a laptop while
checking the rebuild. Those two payloads were copied into the job's state
store with their true retrieval times, keeping the job's own record of its
failed attempt at 16:13. The next run served them and reported both sources
as **stale** ("upstream unreachable at the last attempt; serving stored data
from 1 h 15 m ago"), which was exactly the case. At 16:50 UTC Celestrak
answered the job itself and both groups went fresh on their own.

**Why record it.** State that did not come from the job's own request is
the kind of thing this project exists to be honest about. Nothing on the
page claimed more than was true at any point: the copied data carried its
real age, and the health panel called it stale until the job had confirmed
it. This is the one time state was written by hand. §27 removed the seed
snapshot because a cold start "happens once"; this is what that once looked
like, on a day the upstream was down.

**The address.** `orbital.rodrigopimenta.com` was moved from Cloudflare Pages
to Firebase Hosting at about 16:45 UTC, after the portfolio site's status
check had been switched to read the published document. The certificate was
issued within fifteen minutes. The Cloudflare Pages project and the
scheduler Worker were deleted afterwards.

