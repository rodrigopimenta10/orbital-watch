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
