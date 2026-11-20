/* Orbital Watch: the page's logic, with no DOM in it.
 *
 * Everything the page *decides* lives here -- which copy of the document to
 * show, how old it is, whether a source is still fresh, where a satellite is
 * right now -- so that it can be run under Node and tested without a browser
 * (tests/web/). app.js does the drawing and nothing else.
 *
 * One rule governs all of it: the page trusts timestamps, not labels. The
 * document says what was true when it was written. The page is read later,
 * sometimes much later, so every age and every state is recomputed here
 * against the reader's own clock. A pipeline that has stopped therefore
 * shows up as data getting older, with no help needed from the pipeline.
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.OrbitalWatchCore = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  /* The document shape this page knows how to draw. A document with another
   * version is refused rather than half-rendered: the baked copy shipped with
   * this page is always one it can read. */
  var SCHEMA_VERSION = 2;
  var DOCUMENT_KIND = "orbital-watch.latest";

  /* How long to wait for the live document before showing the baked copy. A
   * few seconds: long enough for a slow mobile connection, short enough that
   * a reader is not left looking at an empty page. */
  var LIVE_TIMEOUT_MS = 4000;

  /* Used only if a document does not carry its own schedule. */
  var DEFAULT_SILENCE_LIMIT_SECONDS = 1800;

  var DEFAULT_CONFIG = {
    liveDocumentUrl: "live/latest.json",
    bakedDocumentUrl: "data/latest.json",
  };

  // ------------------------------------------------------------- small things

  /** Own-property test. Bare `obj[key]` also finds "constructor", "toString". */
  function has(obj, key) {
    return Object.prototype.hasOwnProperty.call(obj, key);
  }

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function isNumber(value) {
    return typeof value === "number" && isFinite(value);
  }

  function parseTime(value) {
    if (!value) return null;
    var t = new Date(value);
    return isNaN(t.getTime()) ? null : t;
  }

  function humanDuration(seconds) {
    if (!isNumber(seconds)) return "unknown";
    var s = Math.abs(Math.round(seconds));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m";
    if (s < 86400) {
      var h = Math.floor(s / 3600);
      var m = Math.floor((s % 3600) / 60);
      return m ? h + "h " + m + "m" : h + "h";
    }
    var d = Math.floor(s / 86400);
    var hh = Math.floor((s % 86400) / 3600);
    return hh ? d + "d " + hh + "h" : d + "d";
  }

  /** "3h 12m ago" / "in 4m" -- relative phrasing an operator reads quickly. */
  function relative(value, now) {
    var t = parseTime(value);
    if (!t) return "unknown";
    var seconds = ((now || new Date()) - t) / 1000;
    var text = humanDuration(seconds);
    return seconds < 0 ? "in " + text : text + " ago";
  }

  function utcStamp(value) {
    var t = value instanceof Date ? value : parseTime(value);
    if (!t || isNaN(t.getTime())) return "unknown";
    return t.toISOString().replace("T", " ").slice(0, 19);
  }

  var COMPASS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
  ];

  /** 16-point compass label for an azimuth in degrees. */
  function compass(azimuthDeg) {
    if (!isNumber(azimuthDeg)) return "—";
    var wrapped = ((azimuthDeg % 360) + 360) % 360;
    return COMPASS[Math.floor(wrapped / 22.5 + 0.5) % 16];
  }

  // ------------------------------------------------------- loading a document

  /**
   * Why a parsed value cannot be drawn as a document, or null if it can.
   *
   * Deliberately shallow. The pipeline validates every field before it
   * publishes; this only has to tell "a document" from "something else that
   * arrived with a 200" -- an error page, a truncated body, a document from a
   * future version of the pipeline.
   */
  function documentProblem(value) {
    if (!isObject(value)) return "the response is not a JSON object";
    if (value.kind !== DOCUMENT_KIND) return "the response is not an Orbital Watch document";
    if (value.schema_version !== SCHEMA_VERSION) {
      return (
        "the document is schema version " +
        String(value.schema_version) +
        ", and this page reads version " +
        SCHEMA_VERSION
      );
    }
    if (!parseTime(value.generated_at)) return "the document carries no generation time";
    var sections = ["meta", "sky", "passes", "space_weather", "health"];
    for (var i = 0; i < sections.length; i += 1) {
      if (!isObject(value[sections[i]])) {
        return "the document has no " + sections[i] + " section";
      }
    }
    return null;
  }

  /**
   * Fetch one URL and resolve to {ok, document, error}. Never rejects.
   *
   * `env` carries the browser facilities this needs (fetch, timers,
   * AbortController), so a test can substitute its own.
   */
  function fetchDocument(url, timeoutMs, env) {
    return new Promise(function (resolve) {
      var settled = false;
      var controller = env.AbortController ? new env.AbortController() : null;

      function settle(result) {
        if (settled) return;
        settled = true;
        env.clearTimeout(timer);
        resolve(result);
      }

      var timer = env.setTimeout(function () {
        // Abort as well as give up, so a response that arrives late is not
        // left downloading in the background.
        if (controller) controller.abort();
        settle({
          ok: false,
          document: null,
          error: "no answer within " + Math.round(timeoutMs / 1000) + "s",
        });
      }, timeoutMs);

      var options = { cache: "no-cache" };
      if (controller) options.signal = controller.signal;

      Promise.resolve()
        .then(function () {
          return env.fetch(url, options);
        })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("HTTP " + response.status);
          }
          return response.json();
        })
        .then(function (parsed) {
          var problem = documentProblem(parsed);
          if (problem) throw new Error(problem);
          settle({ ok: true, document: parsed, error: null });
        })
        .catch(function (error) {
          settle({
            ok: false,
            document: null,
            error: (error && error.message) || String(error),
          });
        });
    });
  }

  /**
   * Get a document to show: the live one, or failing that the baked copy.
   *
   * Resolves to {ok, document, origin, liveError, bakedError}; never rejects.
   * `origin` is "live" or "baked", and the page says which. The baked copy is
   * only asked for when the live one could not be had, so a healthy visit
   * costs one request.
   */
  function loadDocument(config, env) {
    var settings = config || {};
    var liveUrl = settings.liveDocumentUrl || DEFAULT_CONFIG.liveDocumentUrl;
    var bakedUrl = settings.bakedDocumentUrl || DEFAULT_CONFIG.bakedDocumentUrl;
    var timeoutMs = settings.liveTimeoutMs || LIVE_TIMEOUT_MS;

    return fetchDocument(liveUrl, timeoutMs, env).then(function (live) {
      if (live.ok) {
        return {
          ok: true,
          document: live.document,
          origin: "live",
          liveError: null,
          bakedError: null,
        };
      }
      return fetchDocument(bakedUrl, timeoutMs, env).then(function (baked) {
        return {
          ok: baked.ok,
          document: baked.document,
          origin: baked.ok ? "baked" : null,
          liveError: live.error,
          bakedError: baked.error,
        };
      });
    });
  }

  /**
   * Decide what a later load means for a page that is already showing one.
   *
   * Returns {document, origin, changed, refreshError}. The page only ever
   * moves forward: a baked copy never replaces a newer live document just
   * because one poll failed. In that case the page keeps what it has and
   * records that the check failed, so it can say so.
   */
  function adoptLoad(current, loaded) {
    if (!current || !current.document) {
      return {
        document: loaded.ok ? loaded.document : null,
        origin: loaded.ok ? loaded.origin : null,
        changed: true,
        refreshError: loaded.ok ? null : loaded.liveError,
        liveError: loaded.liveError,
        bakedError: loaded.bakedError,
      };
    }

    var held = parseTime(current.document.generated_at);
    var offered = loaded.ok ? parseTime(loaded.document.generated_at) : null;

    if (loaded.ok && loaded.origin === "live") {
      return {
        document: loaded.document,
        origin: "live",
        changed: current.origin !== "live" || +offered !== +held,
        refreshError: null,
        liveError: null,
        bakedError: null,
      };
    }

    // The live document could not be had this time. Keep what is on screen
    // unless the baked copy is actually newer (a deploy happened meanwhile).
    if (loaded.ok && offered > held) {
      return {
        document: loaded.document,
        origin: "baked",
        changed: true,
        refreshError: loaded.liveError,
        liveError: loaded.liveError,
        bakedError: null,
      };
    }
    return {
      document: current.document,
      origin: current.origin,
      changed: false,
      refreshError: loaded.liveError || "the live document could not be loaded",
      liveError: loaded.liveError,
      bakedError: loaded.bakedError,
    };
  }

  // ---------------------------------------------------- the pipeline's pulse

  /**
   * How old the document is and whether that is a problem.
   *
   * The schedule comes from the document itself: how often a new one should
   * appear, and after how long without one the pipeline counts as silent.
   * The age comes from the reader's clock. Nothing the pipeline does or
   * fails to do can stop this from noticing that it has gone quiet.
   */
  function pipelineStatus(doc, now) {
    var generated = parseTime(doc && doc.generated_at);
    var schedule = (doc && doc.meta && doc.meta.pipeline) || {};
    var limit = isNumber(schedule.silence_limit_seconds)
      ? schedule.silence_limit_seconds
      : DEFAULT_SILENCE_LIMIT_SECONDS;
    var age = generated ? Math.max(0, (now - generated) / 1000) : null;
    return {
      generatedAt: generated,
      ageSeconds: age,
      cadenceSeconds: isNumber(schedule.cadence_seconds) ? schedule.cadence_seconds : null,
      silenceLimitSeconds: limit,
      silent: age === null ? true : age > limit,
    };
  }

  // ------------------------------------------------------ freshness, live
  //
  // The pipeline computes each source's state and writes it into the
  // document. That value is correct when written and decays afterwards: if
  // the job stops, a page that trusted it would keep asserting "fresh" while
  // the timestamp beside it counted up into days. Everything needed to
  // recompute the state is in the document -- last_success and the published
  // thresholds -- so it is recomputed, and the panel stays honest however
  // long the page outlives its data.

  var KNOWN_STATES = { fresh: true, stale: true, failed: true, unknown: true };

  /**
   * A source's state as of `now`.
   *
   * `fallbackCopy` is true when the page is showing the baked copy because
   * the live document could not be reached. Nothing in a fallback copy is
   * reported fresh: the page has lost contact with the pipeline, exactly as
   * the pipeline reports a source stale when it has lost contact with an
   * upstream, however good the numbers still look.
   */
  function stateNow(source, now, fallbackCopy) {
    if (!source) return "failed";

    // No data at all is FAILED. The pipeline always pairs a missing payload
    // with a null last_success and records the state it computed.
    if (source.state === "failed" && !source.last_success) return "failed";

    var t = parseTime(source.last_success);
    if (!t) {
      // Data that cannot be dated is STALE, not failed: something real is
      // being served, its age is simply unknown.
      return source.state === "failed" ? "failed" : "stale";
    }

    var thresholds = source.thresholds || {};
    var fresh = thresholds.fresh_within_seconds;
    var stale = thresholds.stale_within_seconds;
    if (!isNumber(fresh) || !isNumber(stale)) {
      return has(KNOWN_STATES, source.state) ? source.state : "unknown";
    }

    var age = (now - t) / 1000;
    if (age > stale) return "failed";
    if (age > fresh) return "stale";
    // Inside the freshness window by age -- but if the pipeline could not
    // reach upstream at its last attempt, it is not fresh.
    if (source.outcome === "cache_fallback") return "stale";
    if (fallbackCopy) return "stale";
    return "fresh";
  }

  /* Worst-first. "unknown" outranks "fresh": a source that cannot be
   * assessed must never let the top-level badge claim everything is fine. */
  function worstState(states) {
    if (states.indexOf("failed") !== -1) return "failed";
    if (states.indexOf("stale") !== -1) return "stale";
    if (states.indexOf("unknown") !== -1) return "unknown";
    return states.length ? "fresh" : "unknown";
  }

  // ------------------------------------------------------------- sky, live
  //
  // The document carries, for each object near the sky, its position
  // relative to the ground station sampled every `step_seconds` over a short
  // window: [east_km, north_km, up_km, altitude_km]. To show the sky as it
  // is when the page is read, this interpolates along that track to the
  // reader's clock.
  //
  // That is all it does. There is no orbit propagation here -- no SGP4, no
  // reference frames, no time systems. Over one step a satellite moves in
  // very nearly a straight line, so a straight line between two published
  // points is within about a kilometre of where SGP4 puts it; the pipeline's
  // tests hold the published track to that. Turning the interpolated vector
  // into the angles an operator reads is two arctangents.

  /** Position on a track at `whenMs`, or null outside it. */
  function trackPosition(track, startMs, stepSeconds, whenMs) {
    if (!Array.isArray(track) || track.length < 2 || !(stepSeconds > 0)) return null;
    var offset = (whenMs - startMs) / (stepSeconds * 1000);
    if (!(offset >= 0) || offset > track.length - 1) return null;

    var index = Math.min(Math.floor(offset), track.length - 2);
    var fraction = offset - index;
    var a = track[index];
    var b = track[index + 1];
    if (!Array.isArray(a) || !Array.isArray(b) || a.length < 4 || b.length < 4) {
      return null;
    }

    function mix(i) {
      return a[i] + (b[i] - a[i]) * fraction;
    }
    var east = mix(0);
    var north = mix(1);
    var up = mix(2);
    var altitude = mix(3);
    if (!isNumber(east) || !isNumber(north) || !isNumber(up) || !isNumber(altitude)) {
      return null;
    }

    var degrees = 180 / Math.PI;
    var azimuth = Math.atan2(east, north) * degrees;
    return {
      elevation_deg: Math.atan2(up, Math.sqrt(east * east + north * north)) * degrees,
      azimuth_deg: (azimuth + 360) % 360,
      range_km: Math.sqrt(east * east + north * north + up * up),
      altitude_km: altitude,
    };
  }

  /**
   * The sky as of `now`: {status, at, coveredUntil, satellites}.
   *
   * status is one of
   *   "current"  -- `now` is inside the published track; positions are real.
   *   "expired"  -- the track has run out. There are NO positions: showing
   *                 the last ones as if they were current is the one thing
   *                 this view must not do.
   *   "early"    -- the reader's clock is before the track starts (a clock
   *                 set wrong). Positions are given for the track's start,
   *                 and `at` says so.
   *   "missing"  -- the document carries no usable track.
   */
  function skyAt(sky, now) {
    var ephemeris = (sky && sky.ephemeris) || {};
    var start = parseTime(ephemeris.start);
    var step = ephemeris.step_seconds;
    var samples = ephemeris.samples;
    if (!start || !isNumber(step) || step <= 0 || !isNumber(samples) || samples < 2) {
      return { status: "missing", at: null, coveredUntil: null, satellites: [] };
    }

    var startMs = start.getTime();
    var endMs = startMs + (samples - 1) * step * 1000;
    var coveredUntil = new Date(endMs);
    var nowMs = now.getTime();

    if (nowMs > endMs) {
      return { status: "expired", at: null, coveredUntil: coveredUntil, satellites: [] };
    }
    var status = nowMs < startMs ? "early" : "current";
    var whenMs = Math.max(nowMs, startMs);

    var horizon = isNumber(sky.horizon_elevation_deg) ? sky.horizon_elevation_deg : 0;
    var visible = [];
    var all = Array.isArray(sky.satellites) ? sky.satellites : [];
    for (var i = 0; i < all.length; i += 1) {
      var satellite = all[i];
      if (!isObject(satellite)) continue;
      var position = trackPosition(satellite.track, startMs, step, whenMs);
      if (!position || position.elevation_deg < horizon) continue;

      var epoch = parseTime(satellite.tle_epoch);
      visible.push({
        name: satellite.name,
        norad_id: satellite.norad_id,
        group: satellite.group,
        elevation_deg: position.elevation_deg,
        azimuth_deg: position.azimuth_deg,
        compass: compass(position.azimuth_deg),
        range_km: position.range_km,
        altitude_km: position.altitude_km,
        // Against the instant the position is for, so the element age
        // describes the same moment as the numbers beside it.
        tle_age_hours: epoch ? (whenMs - epoch.getTime()) / 3600000 : null,
      });
    }
    visible.sort(function (p, q) {
      return q.elevation_deg - p.elevation_deg;
    });

    return {
      status: status,
      at: new Date(whenMs),
      coveredUntil: coveredUntil,
      satellites: visible,
    };
  }

  return {
    SCHEMA_VERSION: SCHEMA_VERSION,
    DOCUMENT_KIND: DOCUMENT_KIND,
    LIVE_TIMEOUT_MS: LIVE_TIMEOUT_MS,
    DEFAULT_CONFIG: DEFAULT_CONFIG,
    has: has,
    isNumber: isNumber,
    parseTime: parseTime,
    humanDuration: humanDuration,
    relative: relative,
    utcStamp: utcStamp,
    compass: compass,
    documentProblem: documentProblem,
    fetchDocument: fetchDocument,
    loadDocument: loadDocument,
    adoptLoad: adoptLoad,
    pipelineStatus: pipelineStatus,
    stateNow: stateNow,
    worstState: worstState,
    trackPosition: trackPosition,
    skyAt: skyAt,
  };
});
