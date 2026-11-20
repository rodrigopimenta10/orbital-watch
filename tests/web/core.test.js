/* Tests for the page's logic (web/assets/core.js).
 *
 * Run with `node --test tests/web/`. Node's own test runner and assert
 * module: no package.json, no dependencies, nothing to install.
 *
 * core.js has no DOM in it, so everything the page *decides* is tested here
 * without a browser: which copy of the document to show, when to say the
 * pipeline has gone quiet, what state a source is in now, and where a
 * satellite is now.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const Core = require("../../web/assets/core.js");

const NOW = new Date("2026-08-06T15:00:00Z");

function minutesAgo(minutes, from = NOW) {
  return new Date(from.getTime() - minutes * 60000).toISOString();
}

function documentAt(generatedAt, extra = {}) {
  return {
    kind: "orbital-watch.latest",
    schema_version: 2,
    generated_at: generatedAt,
    meta: { pipeline: { cadence_seconds: 600, silence_limit_seconds: 1800 } },
    sky: {},
    passes: {},
    space_weather: {},
    health: {},
    ...extra,
  };
}

// ------------------------------------------------------------- fake browser

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => {
      if (body instanceof Error) throw body;
      return body;
    },
  };
}

/**
 * A stand-in for the browser facilities loadDocument uses. `routes` maps a
 * URL to a response, an Error (the request fails), or "hang" (it never
 * answers). Timers do not run by themselves: `fireTimers()` runs them, so a
 * timeout happens when the test says so.
 */
function fakeBrowser(routes) {
  const requested = [];
  const timers = new Map();
  let nextTimer = 1;
  let aborted = 0;

  return {
    requested,
    get aborted() {
      return aborted;
    },
    fireTimers() {
      for (const [id, fn] of [...timers]) {
        timers.delete(id);
        fn();
      }
    },
    env: {
      fetch(url, options) {
        requested.push({ url, options });
        const route = routes[url];
        if (route === "hang") return new Promise(() => {});
        if (route instanceof Error) return Promise.reject(route);
        if (route === undefined) return Promise.resolve(jsonResponse(null, 404));
        return Promise.resolve(route);
      },
      setTimeout(fn) {
        const id = nextTimer++;
        timers.set(id, fn);
        return id;
      },
      clearTimeout(id) {
        timers.delete(id);
      },
      AbortController: class {
        constructor() {
          this.signal = {};
        }
        abort() {
          aborted += 1;
        }
      },
    },
  };
}

const CONFIG = { liveDocumentUrl: "https://live.example/latest.json", bakedDocumentUrl: "data/latest.json" };
const LIVE = CONFIG.liveDocumentUrl;
const BAKED = CONFIG.bakedDocumentUrl;

async function settle() {
  // Let the promise chain inside loadDocument run up to its next await.
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
}

// ------------------------------------------------------ loading: live, baked

test("a healthy visit shows the live document and asks for nothing else", async () => {
  const live = documentAt(minutesAgo(3));
  const browser = fakeBrowser({ [LIVE]: jsonResponse(live), [BAKED]: jsonResponse(documentAt(minutesAgo(90))) });

  const loaded = await Core.loadDocument(CONFIG, browser.env);

  assert.equal(loaded.ok, true);
  assert.equal(loaded.origin, "live");
  assert.deepEqual(loaded.document, live);
  assert.deepEqual(browser.requested.map((r) => r.url), [LIVE]);
  // Revalidated every time, so a cached copy is never shown without asking.
  assert.equal(browser.requested[0].options.cache, "no-cache");
});

for (const [name, route] of [
  ["is unreachable", new TypeError("Failed to fetch")],
  ["answers 503", jsonResponse(null, 503)],
  ["answers 404", jsonResponse(null, 404)],
  ["is not JSON", jsonResponse(new SyntaxError("Unexpected token <"))],
  ["is some other JSON", jsonResponse({ error: "not found" })],
  ["is from a schema this page does not know", jsonResponse(documentAt(minutesAgo(1), { schema_version: 3 }))],
  ["has no health section", jsonResponse(documentAt(minutesAgo(1), { health: null }))],
]) {
  test(`when the live document ${name}, the baked copy is shown and the reason kept`, async () => {
    const baked = documentAt(minutesAgo(200));
    const browser = fakeBrowser({ [LIVE]: route, [BAKED]: jsonResponse(baked) });

    const loaded = await Core.loadDocument(CONFIG, browser.env);

    assert.equal(loaded.ok, true);
    assert.equal(loaded.origin, "baked");
    assert.deepEqual(loaded.document, baked);
    assert.ok(loaded.liveError, "the page must be able to say why it fell back");
    assert.deepEqual(browser.requested.map((r) => r.url), [LIVE, BAKED]);
  });
}

test("a live document that never answers is given up on after the timeout", async () => {
  const baked = documentAt(minutesAgo(200));
  const browser = fakeBrowser({ [LIVE]: "hang", [BAKED]: jsonResponse(baked) });

  const pending = Core.loadDocument(CONFIG, browser.env);
  await settle();
  assert.deepEqual(browser.requested.map((r) => r.url), [LIVE], "must wait for the timeout first");

  browser.fireTimers();
  const loaded = await pending;

  assert.equal(loaded.origin, "baked");
  assert.match(loaded.liveError, /no answer within 4s/);
  assert.equal(browser.aborted, 1, "the abandoned request should be cancelled");
});

test("when neither copy can be loaded, both reasons are reported", async () => {
  const browser = fakeBrowser({ [LIVE]: new TypeError("Failed to fetch"), [BAKED]: jsonResponse(null, 404) });

  const loaded = await Core.loadDocument(CONFIG, browser.env);

  assert.equal(loaded.ok, false);
  assert.equal(loaded.document, null);
  assert.equal(loaded.liveError, "Failed to fetch");
  assert.equal(loaded.bakedError, "HTTP 404");
});

test("loading never rejects, whatever fetch does", async () => {
  const env = fakeBrowser({}).env;
  env.fetch = () => {
    throw new Error("fetch itself threw");
  };

  const loaded = await Core.loadDocument(CONFIG, env);

  assert.equal(loaded.ok, false);
  assert.equal(loaded.liveError, "fetch itself threw");
});

test("with no config the page looks where a local build puts things", async () => {
  const browser = fakeBrowser({ "live/latest.json": jsonResponse(documentAt(minutesAgo(1))) });

  const loaded = await Core.loadDocument(undefined, browser.env);

  assert.equal(loaded.origin, "live");
});

// --------------------------------------------- polling: only ever forwards

test("the first load is adopted as it comes", () => {
  const doc = documentAt(minutesAgo(2));
  const next = Core.adoptLoad(null, { ok: true, document: doc, origin: "live", liveError: null });

  assert.equal(next.document, doc);
  assert.equal(next.origin, "live");
  assert.equal(next.changed, true);
  assert.equal(next.refreshError, null);
});

test("an unchanged live document does not cause a redraw", () => {
  const doc = documentAt(minutesAgo(2));
  const current = { document: doc, origin: "live" };

  const next = Core.adoptLoad(current, { ok: true, document: documentAt(doc.generated_at), origin: "live" });

  assert.equal(next.changed, false);
  assert.equal(next.refreshError, null);
});

test("a newer live document replaces the one on screen", () => {
  const current = { document: documentAt(minutesAgo(12)), origin: "live" };
  const newer = documentAt(minutesAgo(2));

  const next = Core.adoptLoad(current, { ok: true, document: newer, origin: "live" });

  assert.equal(next.document, newer);
  assert.equal(next.changed, true);
});

test("one failed poll does not swap a live document for an older baked copy", () => {
  const live = documentAt(minutesAgo(8));
  const current = { document: live, origin: "live" };
  const baked = documentAt(minutesAgo(600));

  const next = Core.adoptLoad(current, { ok: true, document: baked, origin: "baked", liveError: "HTTP 503" });

  assert.equal(next.document, live, "the page must not go backwards");
  assert.equal(next.origin, "live");
  assert.equal(next.changed, false);
  assert.equal(next.refreshError, "HTTP 503", "but it must say the check failed");
});

test("a failed poll with nothing loadable keeps what is on screen", () => {
  const live = documentAt(minutesAgo(8));
  const current = { document: live, origin: "live" };

  const next = Core.adoptLoad(current, { ok: false, document: null, liveError: "Failed to fetch", bakedError: "HTTP 404" });

  assert.equal(next.document, live);
  assert.equal(next.refreshError, "Failed to fetch");
});

test("a baked copy newer than what is on screen is adopted, as a fallback", () => {
  const current = { document: documentAt(minutesAgo(600)), origin: "baked" };
  const redeployed = documentAt(minutesAgo(5));

  const next = Core.adoptLoad(current, { ok: true, document: redeployed, origin: "baked", liveError: "HTTP 503" });

  assert.equal(next.document, redeployed);
  assert.equal(next.origin, "baked");
  assert.equal(next.changed, true);
});

test("recovering from the baked copy to the live document is a change", () => {
  const stamp = minutesAgo(5);
  const current = { document: documentAt(stamp), origin: "baked" };

  const next = Core.adoptLoad(current, { ok: true, document: documentAt(stamp), origin: "live" });

  assert.equal(next.origin, "live");
  assert.equal(next.changed, true, "the page must redraw to drop its fallback wording");
});

// ----------------------------------------------- the pipeline's pulse

test("a document within three cadences is not flagged", () => {
  const status = Core.pipelineStatus(documentAt(minutesAgo(30)), NOW);

  assert.equal(status.ageSeconds, 1800);
  assert.equal(status.silent, false, "exactly on the limit is still inside it");
});

test("a document older than three cadences means the pipeline has gone quiet", () => {
  const status = Core.pipelineStatus(documentAt(minutesAgo(30.1)), NOW);

  assert.equal(status.silent, true);
  assert.equal(status.cadenceSeconds, 600);
  assert.equal(status.silenceLimitSeconds, 1800);
});

test("the limit comes from the document, not from the page", () => {
  const doc = documentAt(minutesAgo(50));
  doc.meta.pipeline = { cadence_seconds: 1800, silence_limit_seconds: 5400 };

  assert.equal(Core.pipelineStatus(doc, NOW).silent, false);
});

test("a frozen pipeline is noticed with no help from the pipeline", () => {
  // The same bytes, read at later and later times by the same page.
  const doc = documentAt(minutesAgo(5));
  const later = (minutes) => new Date(NOW.getTime() + minutes * 60000);

  assert.equal(Core.pipelineStatus(doc, NOW).silent, false);
  assert.equal(Core.pipelineStatus(doc, later(20)).silent, false);
  assert.equal(Core.pipelineStatus(doc, later(30)).silent, true);
  assert.equal(Core.pipelineStatus(doc, later(60 * 24)).silent, true);
});

test("a reader whose clock is behind does not see a negative age", () => {
  const status = Core.pipelineStatus(documentAt(minutesAgo(-10)), NOW);

  assert.equal(status.ageSeconds, 0);
  assert.equal(status.silent, false);
});

test("a document with no schedule falls back to thirty minutes", () => {
  const doc = documentAt(minutesAgo(31));
  delete doc.meta.pipeline;

  assert.equal(Core.pipelineStatus(doc, NOW).silent, true);
});

// ------------------------------------------------------- freshness, live

function source(overrides = {}) {
  return {
    name: "swpc_kp",
    state: "fresh",
    outcome: "live",
    last_success: minutesAgo(1),
    thresholds: { fresh_within_seconds: 1800, stale_within_seconds: 10800 },
    ...overrides,
  };
}

test("source state is recomputed from its timestamp, not read from its label", () => {
  const labelledFresh = source({ last_success: minutesAgo(1) });
  const at = (minutes) => new Date(NOW.getTime() + minutes * 60000);

  assert.equal(Core.stateNow(labelledFresh, NOW), "fresh");
  assert.equal(Core.stateNow(labelledFresh, at(29)), "fresh", "exactly on the window");
  assert.equal(Core.stateNow(labelledFresh, at(29.1)), "stale");
  assert.equal(Core.stateNow(labelledFresh, at(179)), "stale", "exactly on the limit");
  assert.equal(Core.stateNow(labelledFresh, at(179.1)), "failed");
});

test("lost contact is stale even while the data is young", () => {
  const fallback = source({ state: "stale", outcome: "cache_fallback", last_success: minutesAgo(5) });

  assert.equal(Core.stateNow(fallback, NOW), "stale");
});

test("a deliberate skip inside the politeness floor is fresh", () => {
  const skipped = source({
    outcome: "cache_fresh",
    last_success: minutesAgo(90),
    thresholds: { fresh_within_seconds: 9000, stale_within_seconds: 172800 },
  });

  assert.equal(Core.stateNow(skipped, NOW), "fresh");
});

test("nothing in a fallback copy of the document is reported fresh", () => {
  const young = source({ last_success: minutesAgo(1) });

  assert.equal(Core.stateNow(young, NOW, false), "fresh");
  assert.equal(Core.stateNow(young, NOW, true), "stale");
  // And it does not soften what was already worse.
  const failed = source({ state: "failed", outcome: "failed", last_success: null });
  assert.equal(Core.stateNow(failed, NOW, true), "failed");
});

test("data with no date is stale, never fresh", () => {
  assert.equal(Core.stateNow(source({ last_success: null }), NOW), "stale");
  assert.equal(Core.stateNow(source({ last_success: "not a time" }), NOW), "stale");
});

test("no data at all is failed", () => {
  assert.equal(Core.stateNow(source({ state: "failed", outcome: "failed", last_success: null }), NOW), "failed");
  assert.equal(Core.stateNow(null, NOW), "failed");
});

test("a source without thresholds keeps its published state, or is unknown", () => {
  assert.equal(Core.stateNow(source({ thresholds: undefined, state: "stale" }), NOW), "stale");
  assert.equal(Core.stateNow(source({ thresholds: {}, state: "constructor" }), NOW), "unknown");
});

test("the overall badge is the worst state, and unknown outranks fresh", () => {
  assert.equal(Core.worstState(["fresh", "fresh"]), "fresh");
  assert.equal(Core.worstState(["fresh", "stale"]), "stale");
  assert.equal(Core.worstState(["stale", "failed", "fresh"]), "failed");
  assert.equal(Core.worstState(["fresh", "unknown"]), "unknown");
  assert.equal(Core.worstState([]), "unknown");
});

// ------------------------------------------------------------- sky, live

const CASE = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "track_case.json"), "utf8"));

function separationDegrees(a, b) {
  const rad = Math.PI / 180;
  const cosine =
    Math.sin(a.elevation_deg * rad) * Math.sin(b.elevation_deg * rad) +
    Math.cos(a.elevation_deg * rad) * Math.cos(b.elevation_deg * rad) * Math.cos((a.azimuth_deg - b.azimuth_deg) * rad);
  return Math.acos(Math.min(1, cosine)) / rad;
}

test("interpolated positions match SGP4 between samples, through a pass near the zenith", () => {
  // The fixture is written by the Python pipeline (tests/test_web_fixture.py):
  // a real published track, and SGP4 evaluated directly halfway between its
  // samples -- the instants where interpolation is at its worst.
  assert.ok(CASE.peak_elevation_deg > 80, "the case should be a near-overhead pass");
  const ephemeris = CASE.sky.ephemeris;
  const track = CASE.sky.satellites[0].track;
  const startMs = Date.parse(ephemeris.start);

  let worstAngle = 0;
  let worstRange = 0;
  for (const truth of CASE.sgp4) {
    const got = Core.trackPosition(track, startMs, ephemeris.step_seconds, Date.parse(truth.at));
    assert.ok(got, `no position at ${truth.at}`);
    worstAngle = Math.max(worstAngle, separationDegrees(got, truth));
    worstRange = Math.max(worstRange, Math.abs(got.range_km - truth.range_km));
  }

  assert.ok(CASE.sgp4.length >= 10);
  assert.ok(worstAngle < 0.2, `off by ${worstAngle.toFixed(3)} degrees`);
  assert.ok(worstRange < 2, `range off by ${worstRange.toFixed(2)} km`);
});

test("the sky view shows the object while it is up, at the reader's time", () => {
  const first = new Date(CASE.sgp4[0].at);
  const view = Core.skyAt(CASE.sky, first);

  assert.equal(view.status, "current");
  assert.equal(+view.at, +first);
  assert.equal(view.satellites.length, 1);
  const seen = view.satellites[0];
  assert.equal(seen.name, CASE.sky.satellites[0].name);
  assert.ok(Math.abs(seen.elevation_deg - CASE.sgp4[0].elevation_deg) < 0.2);
  assert.equal(seen.compass, Core.compass(seen.azimuth_deg));
  assert.ok(seen.tle_age_hours > 0);
});

test("an object that has set is no longer listed", () => {
  // The track runs on past the end of the pass: find a sample where the
  // object is below the horizon (its "up" component is negative).
  const ephemeris = CASE.sky.ephemeris;
  const below = CASE.sky.satellites[0].track.findIndex((sample) => sample[2] < 0);
  assert.ok(below > 0, "the fixture track should include the object setting");

  const when = new Date(Date.parse(ephemeris.start) + below * ephemeris.step_seconds * 1000);
  const view = Core.skyAt(CASE.sky, when);

  assert.equal(view.status, "current");
  assert.deepEqual(view.satellites, []);
});

test("past the end of the track there are no positions at all", () => {
  const ephemeris = CASE.sky.ephemeris;
  const end = Date.parse(ephemeris.start) + (ephemeris.samples - 1) * ephemeris.step_seconds * 1000;

  const atEnd = Core.skyAt(CASE.sky, new Date(end));
  const after = Core.skyAt(CASE.sky, new Date(end + 1000));
  const muchLater = Core.skyAt(CASE.sky, new Date(end + 86400000));

  assert.equal(atEnd.status, "current", "the last sample is still inside the track");
  for (const view of [after, muchLater]) {
    assert.equal(view.status, "expired");
    assert.deepEqual(view.satellites, [], "old positions must not be shown as current");
    assert.equal(view.at, null);
    assert.equal(+view.coveredUntil, end);
  }
});

test("a clock that reads before the track starts gets the first positions, and is told", () => {
  const start = Date.parse(CASE.sky.ephemeris.start);
  const view = Core.skyAt(CASE.sky, new Date(start - 3600000));

  assert.equal(view.status, "early");
  assert.equal(+view.at, start);
});

test("a document without a usable track says so instead of guessing", () => {
  for (const sky of [undefined, {}, { ephemeris: {} }, { ephemeris: { start: "x", step_seconds: 30, samples: 61 } }, { ephemeris: { start: CASE.sky.ephemeris.start, step_seconds: 0, samples: 61 } }, { ephemeris: { start: CASE.sky.ephemeris.start, step_seconds: 30, samples: 1 } }]) {
    assert.equal(Core.skyAt(sky, NOW).status, "missing");
  }
});

test("a damaged track costs one object, not the view", () => {
  const start = "2026-08-06T15:00:00Z";
  const up = [[0, 100, 500, 420], [10, 100, 500, 420]];
  const sky = {
    horizon_elevation_deg: 0,
    ephemeris: { start, step_seconds: 30, samples: 2 },
    satellites: [
      { name: "GOOD", group: "stations", tle_epoch: "2026-08-06T03:00:00Z", track: up },
      { name: "SHORT", track: [[0, 100, 500, 420]] },
      { name: "TEXT", track: [[0, "north", 500, 420], [10, 100, 500, 420]] },
      { name: "NOT A LIST", track: "nope" },
      null,
    ],
  };

  const view = Core.skyAt(sky, new Date(Date.parse(start) + 15000));

  assert.deepEqual(view.satellites.map((s) => s.name), ["GOOD"]);
  assert.equal(view.satellites[0].tle_age_hours.toFixed(2), "12.00");
});

test("objects are listed highest first", () => {
  const start = "2026-08-06T15:00:00Z";
  const flat = (up) => [[0, 1000, up, 420], [0, 1000, up, 420]];
  const sky = {
    horizon_elevation_deg: 0,
    ephemeris: { start, step_seconds: 30, samples: 2 },
    satellites: [
      { name: "LOW", track: flat(100) },
      { name: "HIGH", track: flat(3000) },
      { name: "MID", track: flat(800) },
      { name: "BELOW", track: flat(-50) },
    ],
  };

  const names = Core.skyAt(sky, new Date(start)).satellites.map((s) => s.name);

  assert.deepEqual(names, ["HIGH", "MID", "LOW"]);
});

test("a vector straight up is at the zenith, and due east is at 90 degrees", () => {
  const overhead = Core.trackPosition([[0, 0, 400, 400], [0, 0, 400, 400]], 0, 30, 0);
  const east = Core.trackPosition([[1000, 0, 0, 400], [1000, 0, 0, 400]], 0, 30, 0);
  const northWest = Core.trackPosition([[-500, 500, 0, 400], [-500, 500, 0, 400]], 0, 30, 0);

  assert.equal(overhead.elevation_deg, 90);
  assert.equal(overhead.range_km, 400);
  assert.equal(east.azimuth_deg, 90);
  assert.equal(east.elevation_deg, 0);
  assert.equal(northWest.azimuth_deg, 315);
});

// ---------------------------------------------------------------- small things

test("compass labels wrap and tolerate nonsense", () => {
  assert.equal(Core.compass(0), "N");
  assert.equal(Core.compass(11.24), "N");
  assert.equal(Core.compass(11.26), "NNE");
  assert.equal(Core.compass(90), "E");
  assert.equal(Core.compass(359.9), "N");
  assert.equal(Core.compass(450), "E");
  assert.equal(Core.compass(-90), "W");
  assert.equal(Core.compass(NaN), "—");
});

test("durations and relative times read the way an operator scans them", () => {
  assert.equal(Core.humanDuration(45), "45s");
  assert.equal(Core.humanDuration(600), "10m");
  assert.equal(Core.humanDuration(3 * 3600 + 12 * 60), "3h 12m");
  assert.equal(Core.humanDuration(2 * 86400), "2d");
  assert.equal(Core.humanDuration(undefined), "unknown");
  assert.equal(Core.relative(minutesAgo(3), NOW), "3m ago");
  assert.equal(Core.relative(minutesAgo(-4), NOW), "in 4m");
  assert.equal(Core.relative(null, NOW), "unknown");
  assert.equal(Core.utcStamp("2026-08-06T15:00:00+00:00"), "2026-08-06 15:00:00");
  assert.equal(Core.utcStamp("garbage"), "unknown");
});

test("the page's idea of the document matches the pipeline's", () => {
  // Pinned to the Python side by tests/test_repo_conventions.py.
  assert.equal(Core.SCHEMA_VERSION, 2);
  assert.equal(Core.DOCUMENT_KIND, "orbital-watch.latest");
});
