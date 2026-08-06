# claude.md — Orbital Watch

> **You are building a complete, deployable web application from scratch.** Read this entire file before writing code. Everything you need is specified here. Where a decision is left open, it is marked **[YOUR CALL]** — decide, note the decision in `DECISIONS.md`, and move on. Do not ask for clarification on anything not marked that way.

---

## 1. What this is and why it exists

**Orbital Watch** is a public web dashboard that tracks satellites overhead and correlates them with live space-weather conditions.

**The real purpose:** this is a portfolio project for a Systems Engineer whose day job is satellite ground-segment automation, who is interviewing for mission-operations, ground-systems, and platform-engineering roles at aerospace and defense-tech companies. Recruiters and hiring managers will click it. It must look and behave like something an operations engineer built, not a tutorial.

**Design consequences of that purpose — these are requirements, not preferences:**

- **It must always render something useful**, even when an upstream API is down. A blank screen or a stack trace is a failed portfolio project. Degrade visibly and gracefully.
- **It must show its own health.** A status indicator for each data source (fresh / stale / failed, with last-success timestamp) is a first-class feature, not an afterthought. This is the single detail that signals "operations engineer" to anyone in this industry.
- **Every number must be traceable.** Show data age and source next to values. Never display a figure the user cannot account for.
- **No secrets, no paid APIs, no login.** Everything must work from public, unauthenticated data sources.

---

## 2. Hard constraints

| Constraint | Value |
|---|---|
| Language | Python 3.11+ for all backend/data work |
| Package manager | **`uv`** — not pip, not poetry. `uv init`, `uv add`, `uv run`. |
| Linter/formatter | **`ruff`** (both lint and format) |
| Tests | **`pytest`** |
| Frontend | **[YOUR CALL]** — plain HTML/CSS/JS or a light framework. Must build to static files. No SSR, no Node server at runtime. |
| Hosting target | Static files + pre-generated JSON. Must deploy to GitHub Pages, Netlify, or Cloudflare Pages with **no backend server**. |
| Secrets | **None.** If a data source needs a key, do not use it. |
| Python deps | Keep minimal. Justify each one in `DECISIONS.md`. |

### The architectural constraint that shapes everything

There is **no runtime backend**. The site is static. So:

1. A scheduled Python job fetches upstream data, computes everything, and writes **static JSON files** into the build output.
2. The frontend loads those JSON files and renders. It never calls an upstream API directly.

This is deliberate. It means upstream outages cannot break the live site, it costs nothing to host, and the "last successful update" timestamp becomes meaningful. Do not add a live proxy or serverless function.

---

## 3. Data sources (all public, no auth)

Verify each endpoint works before building against it. If one is unreachable, note it in `DECISIONS.md` and implement the fetcher with a clearly-marked fixture fallback so the build still succeeds.

### 3.1 Satellite orbital elements — Celestrak

TLE (two-line element) data. Use the GP API in JSON format.

```
https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=json
https://celestrak.org/NORAD/elements/gp.php?GROUP=starlink&FORMAT=json
https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=json
```

**Celestrak rate-limits aggressively and will block abusive clients.** Therefore:
- Fetch **at most once per hour**, ideally less. TLEs don't change faster than that.
- **Cache to disk** and reuse the cache if the fetch fails.
- Send a descriptive `User-Agent` identifying the project.
- Do **not** fetch the full catalog. Use the specific groups above.

### 3.2 Space weather — NOAA SWPC

Public JSON, no key required.

```
https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json
https://services.swpc.noaa.gov/json/solar-cycle/observed-solar-cycle-indices.json
https://services.swpc.noaa.gov/products/solar-wind/plasma-2-hour.json
```

Kp index is the headline metric: 0-9, where 5+ is a geomagnetic storm.

### 3.3 Orbital propagation

Use **`sgp4`** (the reference SGP4 implementation) or **`skyfield`** to turn TLEs into positions and passes. **[YOUR CALL]** which — `skyfield` is friendlier for pass prediction and topocentric geometry; `sgp4` is lower-level and lighter. Justify in `DECISIONS.md`.

---

## 4. What it must do

### 4.1 Core features (required)

1. **Live sky view** — which tracked satellites are currently above the horizon for a given observer location. Default observer: **Rockville, MD (39.0840° N, 77.1528° W)**. Location must be configurable.

2. **Next passes** — for each tracked satellite, the next N passes over the observer: start time, peak elevation, duration, direction. Sorted by soonest.

3. **Space weather panel** — current Kp index with a plain-English interpretation (quiet / unsettled / active / storm), recent trend, and solar wind speed.

4. **The operational correlation** — this is the feature that makes the project interesting rather than generic. Explain, on the page, that elevated geomagnetic activity increases atmospheric drag on low-Earth-orbit satellites and can degrade HF/satellite links. When Kp is elevated, surface a banner noting that ground-segment operators would expect increased tracking error and potential link degradation. **This is the domain-expertise signal. Do not cut it.**

5. **System health panel** — per data source: last successful fetch (UTC + relative), current state (fresh / stale / failed), and the staleness threshold that defines each state. Make staleness thresholds explicit constants, not magic numbers.

### 4.2 Explicitly out of scope

Do not build: user accounts, a database, notifications/alerting, a full satellite catalog browser, 3D globe rendering, or mobile apps. Scope discipline is part of the evaluation.

---

## 5. Required repo structure

```
orbital-watch/
├── claude.md               # this file
├── README.md               # see §7 — write this last, it matters most
├── DECISIONS.md            # every [YOUR CALL] decision + rationale
├── pyproject.toml          # uv-managed
├── .github/workflows/
│   ├── update-data.yml     # scheduled fetch + rebuild + deploy
│   └── ci.yml              # ruff + pytest on push/PR
├── src/orbital_watch/
│   ├── __init__.py
│   ├── config.py           # observer location, tracked groups, thresholds
│   ├── sources/
│   │   ├── celestrak.py    # TLE fetch + disk cache
│   │   └── swpc.py         # space weather fetch + disk cache
│   ├── propagate.py        # TLE -> positions, passes
│   ├── health.py           # source freshness computation
│   └── build.py            # entry point: writes JSON into the build dir
├── tests/
│   ├── fixtures/           # captured real API responses, committed
│   ├── test_propagate.py
│   ├── test_health.py
│   └── test_sources.py
└── web/                    # frontend source -> static build output
```

---

## 6. Engineering standards — how this must be built

These are the standards the author is judged on professionally. They are the point of the project.

### 6.1 Failure handling is the headline feature

Every network call must:
- Have an explicit timeout. No unbounded waits.
- Catch failure, log it clearly, and **fall back to the disk cache**.
- Record the outcome so the health panel can report it truthfully.
- **Never abort the whole build because one source failed.** Partial data with honest status beats no site.

Write this as a deliberate pattern, not scattered try/except blocks. A small helper that wraps "fetch with timeout, cache, and status reporting" and returns a result object carrying `(data, source_state, last_success_time)` is the right shape.

### 6.2 Tests worth having

Aim for meaningful coverage of logic, not a coverage percentage. Required:
- **Propagation correctness** — at least one known TLE with an assertion against an independently-known result. Use a committed fixture, never a live call.
- **Health/staleness logic** — fresh/stale/failed boundaries, including exact-threshold cases.
- **Graceful degradation** — simulate each source failing and assert the build still completes and reports the failure. **This is the most important test in the project.**
- **No test may make a network call.** All fixtures committed under `tests/fixtures/`.

### 6.3 Logging

Structured, leveled logging. INFO for lifecycle, WARNING for degradation, ERROR for failure. Every log line about a fetch must include the source and elapsed time. No `print()` in library code.

### 6.4 CI/CD

- `ci.yml` — `ruff check`, `ruff format --check`, `pytest` on every push and PR.
- `update-data.yml` — scheduled (hourly at most; **[YOUR CALL]** on exact cadence, justify it against Celestrak's rate limits), plus `workflow_dispatch` for manual runs. Fetches, rebuilds, deploys.
- Deploy via **GitHub Actions to Pages**, using `actions/upload-pages-artifact` and `actions/deploy-pages`. Do **not** use a `gh-pages` branch — a checked-in `.gitignore` on that branch silently drops files.
- Include `concurrency: group: pages, cancel-in-progress: false`.

---

## 7. The README is the deliverable

Most viewers will read the README and never run the code. Write it last, when you know what you actually built.

It must contain, in this order:

1. **One sentence** on what this is, then the **live link**.
2. **A screenshot or GIF** of the working dashboard. Leave a clearly-marked placeholder if you can't capture one.
3. **Why it exists** — the operational framing: what a ground-segment operator actually cares about and why correlating orbital passes with space weather is a real concern rather than a novelty.
4. **Architecture** — a short diagram or bullet flow showing scheduled fetch → static JSON → static frontend, and **why** it's built that way (upstream outages can't take the site down, zero hosting cost, honest freshness reporting).
5. **Reliability behavior** — an explicit section on what happens when each upstream source fails, and how the health panel surfaces it. Recruiters in this space will read this section closely.
6. **Local setup** — exact `uv` commands, copy-pasteable, verified working.
7. **Tests** — how to run them, and what the degradation test proves.
8. **Data sources** — credit Celestrak and NOAA SWPC with links, and state the fetch cadence and that caching is used to respect rate limits.
9. **What's intentionally not built** (§4.2) and why. Showing deliberate scope control reads as senior.

Tone: plain, precise, no marketing language. No emoji beyond at most a couple of section markers.

---

## 8. Definition of done

Do not consider the project complete until every box is checked:

- [ ] `uv run python -m orbital_watch.build` succeeds from a clean clone and writes valid JSON.
- [ ] The build **still succeeds** with network access disabled (falls back to cache/fixtures and reports failure state).
- [ ] `ruff check` and `ruff format --check` pass clean.
- [ ] `pytest` passes, including the graceful-degradation test.
- [ ] The frontend builds to static files and renders correctly against generated JSON.
- [ ] Health panel shows real per-source state and timestamps.
- [ ] The space-weather correlation banner appears when Kp is elevated (verify with a fixture forcing a high Kp).
- [ ] Both GitHub Actions workflows are valid and the CI one passes.
- [ ] `README.md` complete per §7.
- [ ] `DECISIONS.md` records every **[YOUR CALL]** decision with rationale.
- [ ] No secrets, no API keys, no `.env` requirement anywhere.

---

## 9. Working method

1. **Start by verifying the data sources.** Curl each endpoint, confirm the shape, and capture responses into `tests/fixtures/`. Build nothing against an API you haven't seen respond.
2. **Then build the pipeline** (sources → propagate → health → build) with tests as you go.
3. **Then the frontend**, against the JSON the pipeline actually produces.
4. **Then CI/CD.**
5. **Then the README**, once the truth is known.

Commit in logical increments with real messages. Don't produce one enormous commit.

If you hit an upstream API that has changed shape or gone away: implement against a committed fixture, mark it clearly in `DECISIONS.md`, and keep going. Do not stall.

---

# 10. Post-deploy hardening — work items (added Aug 6, 2026, after the first live outage)

The site is live at `orbital.rodrigopimenta.com` and the pipeline works. This
section is the remaining work, in priority order, discovered by an actual
failure rather than by review. **Read all of §10 before starting** — items 1 and
2 interact, and doing 2 without 1 will exceed a hard billing limit.

Two bugs found the same day are already fixed and are recorded here only so they
are not reintroduced:

- **Health classification must stay on wall-clock time unless a clock is
  explicitly pinned.** Threading the build's *start* time into
  `health.evaluate` made sources fetched during the build resolve to a negative
  age, which rendered as "Fetched from upstream in the future ago" on the live
  site. `run_build` now passes `health_now` (the caller's explicit `now`, or
  `None`), and `health.classify` clamps age at zero.
- **`test_seed_carries_a_cold_build_with_no_network` must derive its clock from
  `seed/manifest.json`.** Age is evaluated before outcome in `classify`, so once
  the committed seed ages past a freshness window it reports STALE for being
  old and the seed-specific wording never appears. Against a live clock this
  test passes only for a few hours after each re-seed. Do not "fix" a future
  failure of this test by relaxing the assertion.

---

## 10.1 Move the rebuild trigger off GitHub Actions — HIGHEST PRIORITY

**The failure.** On Aug 6 GitHub Actions went into `major_outage`. The site's
data went stale for over three hours and the staleness banner fired. Cloudflare
— which hosts the site — was healthy the entire time. Only the scheduler was
down, and it took the whole freshness story with it.

Worse: `gh run list --workflow=update-data.yml` returns **zero runs**. The
hourly trigger has never executed once since it was committed, so the deploy
hook path is entirely unverified in production.

**Why this is a design flaw, not bad luck.** `DECISIONS.md` §13 rejected
building in Actions and deploying with `wrangler` partly to avoid "splitting the
build across two providers." But the current topology still spans two providers
— just for the *trigger* instead of the build. The stated principle and the
implementation disagree, and the seam is exactly where it broke.

**Required change.** Replace the GitHub Actions cron with a **Cloudflare Worker
Cron Trigger** that POSTs the existing Pages deploy hook. Host and scheduler
then live on one platform, and Cron Triggers are included on the Workers free
plan.

- Add the Worker under `infra/refresh-worker/` (or similar) with its own
  `wrangler.toml` declaring a `[triggers] crons = [...]` entry.
- Store the deploy hook URL as a Worker **secret** (`wrangler secret put`),
  never in the repo. It remains a capability URL: anyone holding it can trigger
  a build.
- The Worker's `scheduled()` handler does one `fetch(hook, { method: "POST" })`.
  Keep it that small — no dependencies, nothing to audit.
- **Delete `.github/workflows/update-data.yml`** once the Worker is verified
  firing. Do not leave both active; two schedulers means double the builds
  against the budget in §10.2.
- Document the swap in `DECISIONS.md`, explicitly correcting §13's
  two-provider reasoning rather than quietly contradicting it.
- Keep `ci.yml` on GitHub Actions. Tests and linting belong with the code host;
  it is only the *production refresh path* that must not depend on it.

## 10.2 Fix the refresh cadence — the current one exceeds a hard limit

**The current `cron: "0 * * * *"` is not sustainable and must be changed.**

Cloudflare Pages Free allows **500 builds per month**
(<https://developers.cloudflare.com/pages/platform/limits/>). Every build counts,
whether triggered by cron or by a `git push`.

| Cadence | Builds/month | Headroom vs 500 |
|---|---|---|
| Hourly (current) | 720 | **−220 — over the cap** |
| Every 90 min | 480 | 20 — too tight to absorb pushes |
| **Every 2 hours** | **360** | **140 — correct choice** |
| Every 3 hours | 240 | 260 — wastefully conservative |

Hourly overruns the allowance around day 20 of each month, after which rebuilds
simply stop — the site would silently freeze and drift into STALE with no
failure anywhere to look at.

**Set the cadence to every 2 hours** (`0 */2 * * *`). Independent reasons this
is the right number, not just an affordable one:

- Celestrak refreshes GP data **every 2 hours**, so polling faster cannot return
  newer element sets. Hourly was requesting data that did not exist yet.
- It halves pressure on Celestrak's rate limiter (see §10.3).
- It stays inside the 3-hour SWPC freshness window, so a normal run always
  reports FRESH.
- 140 builds/month of headroom absorbs ordinary development pushes.

**Do not "solve" this by upgrading the Cloudflare plan.** A paid plan lifts the
build ceiling, but the ceiling is not the real limit:

- **TLEs cannot get fresher.** Celestrak publishes GP data on a 2-hour cycle, so
  a faster rebuild re-downloads byte-identical element sets.
- **Space weather could be fresher in principle** — solar wind updates by the
  minute — but `SPACE_WEATHER_STALENESS.fresh_within` is 3 hours, so the health
  panel reports FRESH either way. **The page would look identical.**
- **Faster rebuilds make the worst failure mode more likely,** since every build
  is another cold-cache round of Celestrak requests against a rate limiter that
  is already intermittently refusing us (§10.3).

So a paid plan buys no visible change and raises failure risk. The only way to
get genuinely live space weather is **per-source cadence** — poll SWPC often,
TLEs rarely — which requires client-side fetching or a backend and therefore
contradicts the static-site design in §2. Out of scope; record the reasoning so
the question is not reopened.

**Accepted tradeoff, state it in `DECISIONS.md`:** at a 2-hour cadence, *one*
missed run puts space weather at 4 hours and the site reports STALE. That is
correct behaviour, not a regression — the page is telling the truth about its
own data. **Do not widen `SPACE_WEATHER_STALENESS.fresh_within` to hide it.**
The 3-hour window is principled: Kp is a 3-hourly index. Loosening a threshold
to suppress an honest signal would destroy the one thing this project is for.

## 10.3 Make Celestrak failure the expected case, not the exception

**Observed:** in the 22:30 UTC build, all three Celestrak groups timed out at
20s and fell back to the committed seed, while all four SWPC sources succeeded.
The next build at 22:40 got all seven live. So it is intermittent throttling of
Cloudflare's egress, not a hard block.

**Root cause is structural.** `fetch` refuses to re-request a GP group whose
cache is under 6 hours old — but Cloudflare build containers are ephemeral, so
the cache is cold on *every* build and that floor never applies in production.
The politeness guarantee exists only in local runs. `DECISIONS.md` §3
half-acknowledges this ("the schedule, not just the code, has to stay
conservative") without resolving it.

Moving to a 2-hour cadence (§10.2) reduces the pressure but does not fix the
mechanism. Pick one of these and record the choice:

**Option A — commit the TLE snapshot on a schedule (recommended).** A scheduled
job fetches GP data every 6 hours and commits it to the repo; the Pages build
reads from the repo and *never calls Celestrak*. This is the "data in git"
pattern.
- Makes the 6-hour floor real, because a single job owns all fetching.
- Makes builds deterministic and fully reproducible offline.
- Gives free history: the committed snapshots become a time series.
- Cost: the refresher needs write access and its own schedule, and every
  refresh is a commit, which triggers a build — so it must be counted against
  the §10.2 budget, not added on top of it.

**Option B — persist the cache in Cloudflare KV or R2.** The build reads and
writes the cache to KV, so the 6-hour floor works across builds.
- Smaller change to the pipeline's shape and no commit noise.
- Cost: adds a stateful dependency, and the "no backend" claim in the README
  needs rewording to stay accurate.

**Do not** simply raise `HTTP_TIMEOUT_SECONDS`. The request is being throttled,
not running slowly; a longer timeout converts a fast seed fallback into a slow
one and lengthens every build.

## 10.4 Reliability: what to actually harden, and what not to promise

The goal is **honest degradation under every failure**, not "100% uptime."
100% is not purchasable here at any price: Celestrak, NOAA SWPC, and Cloudflare
are all outside our control, and the project's entire thesis is that a system
should report its own health truthfully rather than pretend. A dashboard that
never admits staleness is the failure mode this repo exists to argue against.

The existing design is already correct on the important axis — per-source
isolation, cache fallback, seed fallback, a build that cannot fail, and a health
panel that classifies rather than asserts. **Do not add machinery that
undermines that.** Specifically: do not add retry storms against rate-limited
upstreams, and do not let any new fallback report FRESH.

Genuine gaps worth closing, highest value first:

1. **Nothing watches the watchman.** If the refresh trigger stops firing, the
   only signal is a visitor noticing the banner. Add a dead-man's check: the
   Worker from §10.1 fetches `/data/health.json` after triggering, and if
   `generated_at` is older than ~3× the cadence, it reports loudly — a
   GitHub issue via API, or an email. This is the single highest-value
   reliability addition, and it is the failure that actually happened.
2. **The seed can rot silently.** It ages into FAILED correctly, but nothing
   prompts a refresh. Add a CI check that fails when `seed/manifest.json` is
   older than ~30 days, so the repo tells you before the site does.
3. **A partial build can publish.** Confirm the deploy is atomic: if
   `build` writes `dist/` incrementally and Cloudflare uploads mid-write, a
   half-built site could go live. Build to a temp directory and move it into
   place, or assert every required artifact exists before the step exits
   non-zero.
4. **No synthetic check on the public URL.** Everything verified so far has
   been verified locally or by hand. Add a scheduled check asserting HTTP 200
   *and correct content-type* on `/`, `/data/health.json`, and the other data
   endpoints. A 200 alone proves nothing — an SPA fallback returns 200 with
   `text/html` for a missing asset, which is exactly how the portfolio site
   shipped a blank page the same day.
5. **Timeout budget is unbounded in aggregate.** Seven sources × 20s means a
   worst case near 140s of network wait before propagation starts. Set an
   overall deadline so a degraded-everything build still finishes promptly.

## 10.5 Polish before it goes on LinkedIn Featured and gets left alone

Assume a hiring engineer opens the repo and spends 90 seconds. Optimise for
that, then stop — this project is going into low-maintenance mode.

- **Say the quiet part out loud in the README.** The strongest thing here is
  that the site reports its own degradation honestly. State that as a design
  thesis in the opening lines, above the feature list. Right now a reader has
  to infer it.
- **Screenshot a degraded state, not just a healthy one.** A health panel
  showing STALE with a real explanation demonstrates the thesis far better than
  seven green rows. Most portfolio projects only ever show the happy path.
- **Add a CI status badge** once §10.1 lands and CI is reliably green. Skip it
  while runs are being cancelled by outages — a red badge is worse than none.
- **`DECISIONS.md` is the differentiator; make it findable.** Link it from the
  README's first screenful with one line on why it exists. Most candidates
  cannot show their reasoning at all.
- **Add the two bugs from Aug 6 to `DECISIONS.md`** as short entries — the
  negative-age regression and the time-dependent test. A repo that records its
  own mistakes and the reasoning behind the fixes reads as far more senior than
  one with a clean, silent history.
- **Do not add features.** No new panels, no new sources. The next marginal
  hour is worth more on the AWS/Terraform project, which closes an actual
  screening gap.

## 10.6 Definition of done for §10

- [ ] Rebuilds are triggered by Cloudflare, not GitHub Actions, and the trigger
      has demonstrably fired on schedule at least twice.
- [ ] `update-data.yml` deleted; `ci.yml` retained.
- [ ] Cadence is every 2 hours and the projected monthly build count is
      documented against the 500 limit.
- [ ] One of §10.3 Option A or B implemented, with the choice and its tradeoff
      recorded in `DECISIONS.md`.
- [ ] A dead-man's check exists that notices a missing refresh without a human
      looking at the page.
- [ ] Synthetic check asserts status **and content-type** on every public
      endpoint.
- [ ] `DECISIONS.md` records: the trigger move (correcting §13), the cadence
      change, the Celestrak decision, and the two Aug 6 bugs.
- [ ] `uv run pytest` and `uv run ruff check .` both clean.
- [ ] Live site shows 7/7 fresh after a scheduled — not pushed — rebuild.
