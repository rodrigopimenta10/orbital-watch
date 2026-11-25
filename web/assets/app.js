/* Orbital Watch frontend: drawing.
 *
 * Loads the one document the pipeline publishes and renders it. There is no
 * upstream call here and no framework. The decisions -- which copy of the
 * document to show, how old it is, what state a source is in, where a
 * satellite is now -- are made in core.js, which has no DOM in it and is
 * tested under Node. This file turns those decisions into elements.
 *
 * Three rules govern everything below.
 *
 * 1. A panel whose data is missing says so, in place, with the reason. It
 *    never renders blank and never renders a number it cannot attribute.
 *    Every panel is rendered inside its own try/catch, so one bad field
 *    cannot take out the panels below it -- least of all the health panel,
 *    which is the one that would have explained the failure.
 *
 * 2. Nothing is taken on trust that can be recomputed. Ages, source states
 *    and satellite positions are all worked out against this browser's clock
 *    on every tick, so a pipeline that has stopped is visible here without
 *    any help from the pipeline.
 *
 * 3. The page says which copy of the document it is showing. The live one
 *    comes from the pipeline's public bucket; if that cannot be reached, the
 *    copy baked into the site at deploy time is shown instead, labelled as
 *    such, and nothing in it is reported as fresh.
 */
(function () {
  "use strict";

  var Core = window.OrbitalWatchCore;
  var CONFIG = window.ORBITAL_WATCH_CONFIG || Core.DEFAULT_CONFIG;

  /* How often to look for a newer document. The pipeline publishes every ten
   * minutes and the document may be cached for one, so asking once a minute
   * keeps the page within about two minutes of the newest one without a
   * reload. An unchanged document costs a conditional request and no redraw. */
  var POLL_MS = 60000;

  /* How often the volatile parts are recomputed: relative times, source
   * states, and satellite positions. A satellite in low orbit crosses a
   * degree of sky in about a second near the zenith, so this is short. Only
   * the volatile text is touched -- re-rendering the page to do it would
   * destroy everything the reader was doing: an open <details> snaps shut
   * mid-paragraph, keyboard focus inside a scroll region is dropped, and
   * horizontal scroll jumps back to column one. */
  var RETICK_MS = 5000;

  /* What the page is showing: the document, where it came from, and whether
   * the last check for a newer one worked. */
  var shown = null;

  /* The latest time a server has told us, and when (on a clock that does not
   * jump) it told us. See Core.readerClock for what it is used for. */
  var serverClock = null;

  function steadyMs() {
    return window.performance && typeof window.performance.now === "function"
      ? window.performance.now()
      : Date.now();
  }

  /* The page's working clock. Every "now" on this page comes from here and
   * never straight from the device: it is the device's clock, but not
   * earlier than the document on screen or than a server has said it is. */
  function pageClock() {
    return Core.readerClock({
      deviceMs: Date.now(),
      documentMs: shown && shown.document ? Date.parse(shown.document.generated_at) : null,
      serverMs: serverClock ? serverClock.atMs + (steadyMs() - serverClock.seenAt) : null,
    });
  }

  function clockNow() {
    return pageClock().now;
  }

  /* Remember a server's time, but only ever move the floor forward: a cached
   * response can carry a Date older than one already seen. */
  function noteServerTime(serverTimeMs) {
    if (typeof serverTimeMs !== "number" || !isFinite(serverTimeMs)) return;
    var projected = serverClock
      ? serverClock.atMs + (steadyMs() - serverClock.seenAt)
      : -Infinity;
    if (serverTimeMs > projected) {
      serverClock = { atMs: serverTimeMs, seenAt: steadyMs() };
    }
  }

  /* Closures that refresh one piece of volatile content. Registered during
   * render, replayed on every tick. Cleared before a full re-render so they
   * cannot accumulate or point at detached nodes. */
  var tickables = [];

  function onTick(fn) {
    tickables.push(fn);
  }

  function runTicks(now) {
    for (var i = 0; i < tickables.length; i += 1) {
      try {
        tickables[i](now);
      } catch (error) {
        // One stale closure must not stop the rest from updating.
        if (window.console) console.error("tick failed", error);
      }
    }
  }

  /** A node whose text is recomputed from the clock on every tick. */
  function live(tag, className, compute) {
    var node = el(tag, className, compute(clockNow()));
    onTick(function (now) {
      node.textContent = compute(now);
    });
    return node;
  }

  /** A holder whose contents are rebuilt from the clock on every tick. */
  function liveNode(tag, className, build) {
    var holder = el(tag, className);
    holder.appendChild(build(clockNow()));
    onTick(function (now) {
      clear(holder);
      holder.appendChild(build(now));
    });
    return holder;
  }

  // ---------------------------------------------------------------- utils

  var has = Core.has;
  var parseTime = Core.parseTime;
  var humanDuration = Core.humanDuration;
  var relative = Core.relative;
  var utcStamp = Core.utcStamp;

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function showError(container, title, detail) {
    clear(container);
    var box = el("div", "banner error-box");
    box.appendChild(el("h3", null, title));
    box.appendChild(el("p", null, detail));
    container.appendChild(box);
  }

  /** Render a panel so that a throw inside it cannot reach the other panels. */
  function guard(container, label, render) {
    try {
      render();
    } catch (error) {
      showError(
        container,
        label + " could not be rendered",
        (error && error.message ? error.message : String(error)) +
          ". The document loaded, so this is a rendering fault rather than a " +
          "data outage."
      );
    }
  }

  /** Format a number, or "—" if it is missing or not finite. */
  function num(value, digits) {
    if (typeof value !== "number" || !isFinite(value)) return "—";
    var places = digits === undefined ? 0 : digits;
    var text = value.toFixed(places);
    // (-0.04).toFixed(1) is "-0.0" and (-0.4).toFixed(0) is "-0". NOAA reports
    // Bz values that small routinely, and "-0.0 nT" reads as a rendering bug.
    if (/^-0(\.0*)?$/.test(text)) text = text.slice(1);
    return text;
  }

  /** Thousands-separated integer, locale-stable so it reads the same anywhere. */
  function int(value) {
    if (typeof value !== "number" || !isFinite(value)) return "—";
    return Math.round(value)
      .toString()
      .replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function humanSeconds(seconds) {
    if (typeof seconds !== "number" || !isFinite(seconds)) return "unknown";
    return humanDuration(seconds);
  }

  /** Status glyphs double the colour cue with a shape, for greyscale and CVD. */
  var STATUS_GLYPH = {
    fresh: "●",
    stale: "◐",
    failed: "▲",
    unknown: "○",
  };

  function badge(cls, glyph, label) {
    var span = el("span", "status " + cls);
    var mark = el("span", "glyph", glyph);
    // Decorative: it restates the colour. A screen reader announcing
    // "black up-pointing triangle failed" is noise.
    mark.setAttribute("aria-hidden", "true");
    span.appendChild(mark);
    span.appendChild(el("span", null, label));
    return span;
  }

  function statusBadge(state) {
    var key = has(STATUS_GLYPH, state) ? state : "unknown";
    return badge("status-" + key, STATUS_GLYPH[key], key);
  }

  /* Geomagnetic severity gets its own vocabulary and its own glyphs. It shares
   * the status *colours* with the health panel -- both are severity scales --
   * but reusing the words (a "fresh" storm?) would conflate two unrelated
   * ideas on the same screen. */
  var SEVERITY = {
    quiet: { label: "nominal", glyph: "●", cls: "status-fresh" },
    elevated: { label: "elevated", glyph: "◆", cls: "status-elevated" },
    storm: { label: "storm", glyph: "▲", cls: "status-failed" },
    unknown: { label: "unknown", glyph: "○", cls: "status-unknown" },
  };

  function severityBadge(severity) {
    var spec = has(SEVERITY, severity) ? SEVERITY[severity] : SEVERITY.unknown;
    return badge(spec.cls, spec.glyph, spec.label);
  }

  /** A source's state right now, given which copy of the document is shown. */
  function stateNow(source, now) {
    return Core.stateNow(source, now, shown && shown.origin === "baked");
  }

  var worstState = Core.worstState;

  // ------------------------------------------------- which copy, and how old

  /* Said only when it matters: ages on this page are worked out from a clock,
   * and if the device's is clearly behind, the reader should know that the
   * page is not using it as it stands. */
  function clockNote() {
    var behind = pageClock().behindSeconds;
    if (behind < 60) return "";
    return (
      " This device's clock appears to be at least " +
      humanDuration(behind) +
      " slow, so times here are worked out from the later of it, the document, " +
      "and the server's own clock."
    );
  }

  /** One sentence saying which copy of the document is on screen. */
  function sourceLine(now) {
    if (!shown || !shown.document) return "";
    var stamp = utcStamp(shown.document.generated_at);
    var age = relative(shown.document.generated_at, now);

    if (shown.origin === "baked") {
      return (
        "Showing the copy of the data that was baked into this site when it " +
        "was deployed, published " +
        stamp +
        " UTC (" +
        age +
        "). The live document could not be loaded (" +
        (shown.liveError || "unknown error") +
        "), so nothing below is reported as fresh." +
        clockNote()
      );
    }
    var text = "Showing the live document, published " + stamp + " UTC (" + age + ").";
    text += clockNote();
    if (shown.refreshError) {
      text +=
        " The last check for a newer one failed (" +
        shown.refreshError +
        "), so this may not be the latest.";
    }
    return text;
  }

  function renderSilenceBanner(container, doc, now) {
    clear(container);
    var status = Core.pipelineStatus(doc, now);
    if (!status.generatedAt || !status.silent) return;

    var age = humanDuration(status.ageSeconds);
    var stamp = utcStamp(status.generatedAt);
    var cadence = status.cadenceSeconds ? humanDuration(status.cadenceSeconds) : null;
    var baked = shown && shown.origin === "baked";

    var banner = el("div", "banner banner-elevated");
    var heading = el("h3");
    heading.appendChild(
      badge("status-elevated", "◆", baked ? "old copy" : "pipeline silent")
    );
    heading.appendChild(
      el(
        "span",
        null,
        baked
          ? "This copy of the data was published at " + stamp + " UTC, " + age + " ago"
          : "The pipeline last published at " + stamp + " UTC, " + age + " ago"
      )
    );
    banner.appendChild(heading);

    var expectation = cadence
      ? "A new document is expected every " + cadence + ". "
      : "";
    banner.appendChild(
      el(
        "p",
        null,
        (baked
          ? expectation +
            "This page could not reach the live document and is showing the " +
            "copy shipped with the site, so the pipeline may be running " +
            "normally while this page cannot see it. "
          : expectation +
            "None has appeared for " +
            age +
            ", so the scheduled job has probably stopped or cannot publish. ") +
          "Everything below is what was true at " +
          stamp +
          " UTC and is simply that old. Source states are recomputed in your " +
          "browser from the published timestamps, so they degrade with age " +
          "rather than staying frozen, and satellite positions are withheld " +
          "once the published track runs out."
      )
    );
    container.appendChild(banner);
  }

  // ----------------------------------------------------------- correlation

  function renderCorrelation(container, payload, kpState) {
    clear(container);
    var c = Core.assessmentNow(payload.correlation, kpState);
    if (!c) {
      showError(
        container,
        "Assessment unavailable",
        "No correlation data in the document."
      );
      return;
    }

    var banner = el("div", "banner banner-" + (c.severity || "unknown"));

    var heading = el("h3");
    heading.appendChild(severityBadge(c.severity));
    heading.appendChild(el("span", null, c.headline || "Unknown"));
    banner.appendChild(heading);

    banner.appendChild(el("p", null, c.detail || ""));

    if (c.operational_impacts && c.operational_impacts.length) {
      var list = el("ul");
      c.operational_impacts.forEach(function (impact) {
        list.appendChild(el("li", null, impact));
      });
      banner.appendChild(list);
    }

    if (c.explanation) {
      var why = el("details", "why");
      var summary = el("summary", null, "Why this matters");
      why.appendChild(summary);
      why.appendChild(el("p", null, c.explanation));
      banner.appendChild(why);
    }

    container.appendChild(banner);
  }

  // -------------------------------------------------------------- weather

  function tile(label, value, unit, meta, provenance) {
    var card = el("div", "card tile");
    card.appendChild(el("div", "label", label));
    var v = el("div", "value", value);
    if (unit) v.appendChild(el("span", "unit", unit));
    card.appendChild(v);
    if (meta) card.appendChild(typeof meta === "string" ? el("div", "meta", meta) : meta);
    if (provenance) {
      card.appendChild(
        typeof provenance === "string" ? el("div", "provenance", provenance) : provenance
      );
    }
    return card;
  }

  /** "NOAA SWPC · <observation time> (<age>)", with the age kept current. */
  function observedAt(time) {
    if (!time) return "NOAA SWPC · no data";
    return live("div", "provenance", function (now) {
      return "NOAA SWPC · " + utcStamp(time) + " (" + relative(time, now) + ")";
    });
  }

  function renderWeatherTiles(container, payload, meta, sky) {
    clear(container);
    var w = payload.weather || {};

    if (typeof w.kp === "number") {
      container.appendChild(
        tile(
          "Planetary Kp",
          num(w.kp, 2),
          null,
          (w.kp_description || "") + " · trend " + (w.kp_trend || "unknown"),
          observedAt(w.kp_time)
        )
      );
    } else {
      container.appendChild(
        tile("Planetary Kp", "—", null, "Unavailable", observedAt(null))
      );
    }

    container.appendChild(
      tile(
        "Solar wind speed",
        num(w.solar_wind_speed_km_s, 0),
        "km/s",
        null,
        observedAt(w.solar_wind_time)
      )
    );

    container.appendChild(
      tile(
        "IMF Bz",
        num(w.bz_nt, 1),
        "nT",
        typeof w.bt_nt === "number" ? "Bt " + num(w.bt_nt, 1) + " nT" : null,
        observedAt(w.mag_time)
      )
    );

    // F10.7 is the standard proxy for solar EUV heating of the thermosphere,
    // and thus the usual driver of neutral density in drag models. It belongs
    // in the drag story more directly than Bz does.
    container.appendChild(
      tile(
        "F10.7 solar flux",
        num(w.f107_flux, 0),
        "sfu",
        typeof w.sunspot_number === "number"
          ? "Sunspot number " + num(w.sunspot_number, 0)
          : null,
        w.solar_cycle_month
          ? "NOAA SWPC · monthly mean, " + w.solar_cycle_month
          : "NOAA SWPC · no data"
      )
    );

    var counts = (meta && meta.counts) || {};
    container.appendChild(
      tile(
        "Tracked objects",
        int(counts.tracked_satellites),
        null,
        // Counted from the same interpolated positions the sky view shows, so
        // the two can never disagree -- and blank, not frozen, once the
        // published track has run out.
        live("div", "meta", function (now) {
          var view = Core.skyAt(sky || {}, now);
          if (view.status === "expired") return "Positions no longer current";
          if (view.status === "missing") return "";
          return view.satellites.length + " above horizon now";
        }),
        "Celestrak GP · propagated with SGP4"
      )
    );
  }

  // ------------------------------------------------------------- Kp chart
  //
  // Bar chart: Kp is a banded 3-hourly index, and bar height is the primary
  // encoding of magnitude. Colour restates the NOAA severity band and is
  // deliberately redundant -- adjacent status hues are close enough that they
  // must not be the only cue, so the labelled storm threshold line and the
  // y-axis carry the real meaning.

  var SVG_NS = "http://www.w3.org/2000/svg";

  function svgEl(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    for (var key in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, key)) {
        node.setAttribute(key, attrs[key]);
      }
    }
    return node;
  }

  function kpColor(kp) {
    if (kp >= 7) return "var(--fill-critical)";
    if (kp >= 5) return "var(--fill-serious)";
    if (kp >= 4) return "var(--fill-warning)";
    return "var(--fill-good)";
  }

  function renderKpChart(card, payload) {
    clear(card);
    var w = payload.weather || {};
    var history = (w.kp_history || []).filter(function (p) {
      return p && typeof p.kp === "number" && isFinite(p.kp) && parseTime(p.time);
    });

    card.appendChild(el("div", "label", "Planetary Kp — recent history"));

    if (!history.length) {
      card.appendChild(el("p", "empty", "No Kp history available in this document."));
      return;
    }

    var width = Math.max(640, Math.min(1100, history.length * 16));
    var height = 220;
    var pad = { top: 16, right: 16, bottom: 34, left: 34 };
    var plotW = width - pad.left - pad.right;
    var plotH = height - pad.top - pad.bottom;
    var maxKp = 9;

    var wrap = el("div", "chart-wrap");
    // Scrollable on narrow screens rather than shrink-to-fit: letterboxing an
    // 896x220 viewBox into a 288px phone column renders the 10px axis labels
    // at ~3px, which is worse than a horizontal scroll.
    wrap.setAttribute("tabindex", "0");
    wrap.setAttribute("role", "region");
    wrap.setAttribute("aria-label", "Planetary Kp index history chart");

    var svg = svgEl("svg", {
      width: width,
      height: height,
      viewBox: "0 0 " + width + " " + height,
      role: "img",
      "aria-label":
        "Bar chart of the planetary Kp index over " +
        history.length +
        " three-hourly observations. Latest value " +
        num(w.kp, 2) +
        (w.kp_label ? ", " + w.kp_label : "") +
        ".",
    });

    function y(kp) {
      return pad.top + plotH - (kp / maxKp) * plotH;
    }

    [0, 3, 5, 7, 9].forEach(function (value) {
      svg.appendChild(
        svgEl("line", {
          x1: pad.left,
          x2: pad.left + plotW,
          y1: y(value),
          y2: y(value),
          stroke: "var(--grid)",
          "stroke-width": 1,
        })
      );
      var label = svgEl("text", {
        x: pad.left - 7,
        y: y(value) + 4,
        "text-anchor": "end",
        fill: "var(--text-muted)",
        "font-size": "10",
      });
      label.textContent = value;
      svg.appendChild(label);
    });

    var slot = plotW / history.length;
    var barW = Math.max(3, slot - 2);

    history.forEach(function (point, index) {
      // Clamped: NOAA caps Kp at 9, but a bar drawn from an out-of-range value
      // would escape the viewBox and be silently clipped rather than noticed.
      var kp = Math.max(0, Math.min(maxKp, point.kp));
      var barH = Math.max(1, (kp / maxKp) * plotH);
      var x = pad.left + index * slot + (slot - barW) / 2;
      var rect = svgEl("rect", {
        x: x,
        y: pad.top + plotH - barH,
        width: barW,
        height: barH,
        rx: Math.min(2, barW / 2),
        fill: kpColor(kp),
      });
      var title = svgEl("title");
      title.textContent = utcStamp(point.time) + " — Kp " + num(point.kp, 2);
      rect.appendChild(title);
      svg.appendChild(rect);
    });

    // Left-anchored: the newest (and during a storm, tallest) bars sit at the
    // right-hand end, and a right-anchored label collides with them exactly
    // when the threshold matters most.
    svg.appendChild(
      svgEl("line", {
        x1: pad.left,
        x2: pad.left + plotW,
        y1: y(5),
        y2: y(5),
        stroke: "var(--status-critical)",
        "stroke-width": 1.5,
        "stroke-dasharray": "4 3",
      })
    );
    var thresholdLabel = svgEl("text", {
      x: pad.left + 4,
      y: y(5) - 5,
      "text-anchor": "start",
      fill: "var(--status-critical)",
      "font-size": "10",
      "font-weight": "600",
    });
    thresholdLabel.textContent = "G1 storm threshold (Kp 5)";
    svg.appendChild(thresholdLabel);

    svg.appendChild(
      svgEl("line", {
        x1: pad.left,
        x2: pad.left + plotW,
        y1: y(0),
        y2: y(0),
        stroke: "var(--axis)",
        "stroke-width": 1,
      })
    );

    if (history.length > 1) {
      var first = svgEl("text", {
        x: pad.left,
        y: height - 12,
        fill: "var(--text-muted)",
        "font-size": "10",
      });
      first.textContent = utcStamp(history[0].time).slice(0, 16) + " UTC";
      svg.appendChild(first);

      var last = svgEl("text", {
        x: pad.left + plotW,
        y: height - 12,
        "text-anchor": "end",
        fill: "var(--text-muted)",
        "font-size": "10",
      });
      last.textContent =
        utcStamp(history[history.length - 1].time).slice(0, 16) + " UTC";
      svg.appendChild(last);
    }

    wrap.appendChild(svg);
    card.appendChild(wrap);
    card.appendChild(
      el(
        "div",
        "chart-caption",
        history.length +
          " three-hourly observations. Bar height is the Kp value, and colour " +
          "restates the NOAA severity band. Hover a bar for its timestamp."
      )
    );
  }

  // ------------------------------------------------------------- tables

  function buildTable(columns, ariaLabel, captionText) {
    var scroll = el("div", "table-scroll");
    // Focusable so the horizontal scroll is reachable by keyboard.
    scroll.setAttribute("tabindex", "0");
    scroll.setAttribute("role", "region");
    scroll.setAttribute("aria-label", ariaLabel);

    var table = el("table");
    var caption = el("caption", "visually-hidden", captionText);
    table.appendChild(caption);

    var thead = el("thead");
    var headRow = el("tr");
    columns.forEach(function (col) {
      var th = el("th", col.numeric ? "num" : null, col.label);
      th.setAttribute("scope", "col");
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = el("tbody");
    table.appendChild(tbody);
    scroll.appendChild(table);
    return { scroll: scroll, tbody: tbody };
  }

  function groupTag(name) {
    var cell = el("td");
    cell.appendChild(el("span", "group-tag", name));
    return cell;
  }

  function coverageNote(meta) {
    var coverage = (meta && meta.coverage) || [];
    if (!coverage.length) return null;
    var parts = coverage.map(function (g) {
      // A group whose element sets could not be had this run is said to be
      // missing, not counted as "all 0".
      if (!g.tracked) return "no " + g.label.toLowerCase() + " (element sets unavailable)";
      return g.sampled
        ? int(g.tracked) + " of " + int(g.available) + " " + g.label.toLowerCase()
        : "all " + int(g.tracked) + " " + g.label.toLowerCase();
    });
    return (
      "Tracking " +
      parts.join(", ") +
      ". Sampled groups keep the most recent element sets, so passes shown are " +
      "the next among tracked objects rather than the next overall."
    );
  }

  /* The sky view is the one panel that is rebuilt on every tick, because it
   * is the one whose *contents* change with the clock: rows appear as
   * objects rise, drop out as they set, and reorder as elevations change.
   * The scroll container is kept and only the rows inside it are replaced,
   * so a reader's horizontal scroll and keyboard focus survive. */
  function renderSky(container, sky, meta) {
    clear(container);
    var holder = el("div");
    var caption = el("div", "provenance");
    var table = null;

    function message(text) {
      table = null;
      clear(holder);
      holder.appendChild(el("p", "empty", text));
    }

    function update(now) {
      var view = Core.skyAt(sky, now);

      if (view.status === "missing") {
        message(
          "No position track in this document, so the sky view cannot be " +
            "shown. Check system health below."
        );
        caption.textContent = "";
        return;
      }

      if (view.status === "expired") {
        // The published track covers a fixed window. Past its end there is
        // nothing to interpolate, and the last known positions are not
        // where anything is now -- so they are not shown at all.
        message(
          "Positions are no longer current. The track in this document " +
            "covers up to " +
            utcStamp(view.coveredUntil) +
            " UTC, which was " +
            relative(view.coveredUntil, now) +
            ", and no newer document has arrived. Satellites have moved on, and " +
            "showing where they were would be showing them in the wrong place."
        );
        caption.textContent = "";
        return;
      }

      if (!view.satellites.length) {
        message(
          "No tracked satellites are above the horizon right now, or no " +
            "orbital data was available. Check system health below."
        );
      } else {
        if (!table) {
          clear(holder);
          table = buildTable(
            [
              { label: "Satellite" },
              { label: "Group" },
              { label: "Elev", numeric: true },
              { label: "Az", numeric: true },
              { label: "Dir", numeric: true },
              { label: "Range", numeric: true },
              { label: "Alt", numeric: true },
              { label: "Elem. age", numeric: true },
            ],
            "Satellites above the horizon",
            "Tracked satellites currently above the horizon, highest elevation first."
          );
          holder.appendChild(table.scroll);
        }
        clear(table.tbody);
        view.satellites.forEach(function (s) {
          var row = el("tr");
          row.appendChild(el("td", "name", s.name || "unknown"));
          row.appendChild(groupTag(s.group || "—"));
          row.appendChild(el("td", "num", num(s.elevation_deg, 1) + "°"));
          row.appendChild(el("td", "num", num(s.azimuth_deg, 0) + "°"));
          row.appendChild(el("td", "num dim", s.compass || "—"));
          row.appendChild(el("td", "num", int(s.range_km) + " km"));
          row.appendChild(el("td", "num", int(s.altitude_km) + " km"));
          row.appendChild(el("td", "num dim", num(s.tle_age_hours, 1) + " h"));
          table.tbody.appendChild(row);
        });
      }

      caption.textContent =
        "Positions as of " +
        utcStamp(view.at) +
        " UTC, by your device's clock" +
        (view.status === "early"
          ? " — which reads earlier than the time this track starts, so the " +
            "first published positions are shown"
          : "") +
        ". Track published through " +
        utcStamp(view.coveredUntil) +
        " UTC.";
    }

    container.appendChild(holder);
    update(clockNow());
    onTick(update);

    container.appendChild(caption);
    container.appendChild(
      el(
        "div",
        "provenance",
        "The pipeline propagates Celestrak element sets with SGP4 and " +
          "publishes a short track per object. This page interpolates along " +
          'it to the current time and does no orbital mechanics itself. "Elem. ' +
          'age" is the age of the element set — propagation error grows with it.'
      )
    );
    var note = coverageNote(meta);
    if (note) container.appendChild(el("div", "provenance", note));
  }

  function renderPasses(container, payload, meta, generatedAt, now) {
    clear(container);
    var all = (payload.passes || []).filter(function (p) {
      return p && p.start;
    });

    // The schedule is computed when the document is generated and read some
    // time later, so part of it may already be history. Showing "18m ago"
    // under a column headed "In" reads as a bug even though the data is fine.
    var upcoming = all.filter(function (p) {
      var end = parseTime(p.end);
      return !(end && end < now);
    });

    // An honest footnote when the search was cut short at the run's deadline.
    function incompleteNote() {
      if (payload.complete !== false) return null;
      return el(
        "div",
        "provenance",
        "The pass search stopped at the run's deadline after " +
          int(payload.satellites_searched) +
          " of " +
          int(payload.satellites_total) +
          " tracked objects, so this list is incomplete."
      );
    }

    if (!upcoming.length) {
      container.appendChild(
        el(
          "p",
          "empty",
          all.length
            ? "All " +
              all.length +
              " predicted passes in this document have already elapsed. The " +
              "page is waiting on a newer document."
            : "No passes predicted, or no orbital data was available. Check " +
              "system health below."
        )
      );
      container.appendChild(
        el("div", "provenance", "Computed " + utcStamp(generatedAt) + " UTC.")
      );
      var cutShort = incompleteNote();
      if (cutShort) container.appendChild(cutShort);
      return;
    }

    var built = buildTable(
      [
        { label: "Satellite" },
        { label: "Group" },
        { label: "Start (UTC)" },
        { label: "In", numeric: true },
        { label: "Peak elev", numeric: true },
        { label: "Duration", numeric: true },
        { label: "Track" },
      ],
      "Upcoming satellite passes",
      "Upcoming passes over the ground station, soonest first."
    );

    upcoming.forEach(function (p) {
      var row = el("tr");
      row.appendChild(el("td", "name", p.name || "unknown"));
      row.appendChild(groupTag(p.group || "—"));
      row.appendChild(el("td", null, utcStamp(p.start).slice(0, 16)));
      row.appendChild(
        live("td", "num dim", function (t) {
          var start = parseTime(p.start);
          var end = parseTime(p.end);
          // A pass that has begun but not ended is neither "in 3m" nor
          // "3m ago" -- it is happening. Saying so is both accurate and the
          // most operationally useful thing the column can show.
          if (start && end && start <= t && t <= end) return "in progress";
          return relative(p.start, t);
        })
      );
      row.appendChild(el("td", "num", num(p.peak_elevation_deg, 0) + "°"));
      row.appendChild(el("td", "num", num(p.duration_minutes, 1) + " min"));
      row.appendChild(
        el(
          "td",
          "dim",
          (p.start_compass || "?") +
            " → " +
            (p.peak_compass || "?") +
            " → " +
            (p.end_compass || "?")
        )
      );
      // The table is built once; rows retire themselves as time passes.
      onTick(function (t) {
        var end = parseTime(p.end);
        row.hidden = !!(end && end < t);
      });
      built.tbody.appendChild(row);
    });

    container.appendChild(built.scroll);
    container.appendChild(
      el(
        "div",
        "provenance",
        "Minimum elevation " +
          num(payload.min_elevation_deg, 0) +
          "°, searched " +
          num(payload.search_hours, 0) +
          " h ahead from " +
          utcStamp(generatedAt) +
          " UTC."
      )
    );
    container.appendChild(
      live("div", "provenance", function (t) {
        var gone = all.filter(function (item) {
          var end = parseTime(item.end);
          return end && end < t;
        }).length;
        if (!gone) return "";
        return (
          gone +
          " of " +
          all.length +
          " passes in this document have already elapsed and are hidden."
        );
      })
    );
    var incomplete = incompleteNote();
    if (incomplete) container.appendChild(incomplete);
    var note = coverageNote(meta);
    if (note) container.appendChild(el("div", "provenance", note));
  }

  // --------------------------------------------------------------- health

  /** The schedule and politeness figures the thresholds are derived from. */
  function scheduleNote(meta) {
    var pipeline = (meta && meta.pipeline) || {};
    var parameters = (meta && meta.parameters) || {};
    var parts = [];
    if (Core.isNumber(pipeline.cadence_seconds)) {
      parts.push("The job runs every " + humanDuration(pipeline.cadence_seconds));
    }
    if (Core.isNumber(pipeline.silence_limit_seconds)) {
      parts.push(
        "a document older than " +
          humanDuration(pipeline.silence_limit_seconds) +
          " is flagged at the top of this page"
      );
    }
    var text = parts.length ? parts.join(", and ") + ". " : "";
    if (Core.isNumber(parameters.celestrak_min_refetch_seconds)) {
      text +=
        "NOAA SWPC is asked on every run. Each Celestrak group is asked at " +
        "most once every " +
        humanDuration(parameters.celestrak_min_refetch_seconds) +
        ", which is how often Celestrak publishes" +
        (Core.isNumber(parameters.celestrak_retry_seconds)
          ? ", and not again for " +
            humanDuration(parameters.celestrak_retry_seconds) +
            " after a failed request"
          : "") +
        ". ";
    }
    return text;
  }

  function renderHealth(container, payload, meta) {
    clear(container);
    var sources = payload.sources || [];

    if (!sources.length) {
      showError(
        container,
        "Health unavailable",
        "The document carries no source health records."
      );
      return;
    }

    // Per-state counts are computed by the live summary at the foot of this
    // panel rather than here, so they stay correct as states age.
    sources.forEach(function (source) {
      var row = el("div", "health-row");

      var left = el("div");
      left.appendChild(el("div", "health-name", source.label || source.name));
      left.appendChild(el("div", "health-url", source.url || ""));
      row.appendChild(left);

      var middle = el("div");
      middle.appendChild(
        liveNode("div", null, function (t) {
          return statusBadge(stateNow(source, t));
        })
      );
      middle.appendChild(
        live("div", "health-when", function (t) {
          return source.last_success
            ? relative(source.last_success, t)
            : "never succeeded";
        })
      );
      row.appendChild(middle);

      var right = el("div");
      // Prefixed deliberately. This sentence was written by the pipeline and
      // can contain an age fixed in the text ("answered 1h 3m ago"), which
      // would otherwise sit next to a live, ticking age and contradict it.
      // Saying whose account it is, and from when, reconciles the two.
      if (source.detail) {
        right.appendChild(
          el("div", "health-detail", "When published: " + source.detail)
        );
      }

      // If age, or having fallen back to the baked copy, has pushed the state
      // past what the pipeline recorded, say so rather than silently
      // disagreeing with the detail text above.
      right.appendChild(
        live("div", "health-when", function (t) {
          var current = stateNow(source, t);
          if (!source.state || current === source.state) return "";
          return (
            "Recomputed in your browser: was " +
            source.state +
            " when published, now " +
            current +
            (shown && shown.origin === "baked" && source.state === "fresh"
              ? " (this is a fallback copy, and nothing in it is reported fresh)."
              : ".")
          );
        })
      );

      if (source.last_success) {
        right.appendChild(
          el(
            "div",
            "health-when",
            "Upstream last answered " + utcStamp(source.last_success) + " UTC"
          )
        );
      }
      var thresholds = source.thresholds || {};
      right.appendChild(
        el(
          "div",
          "health-when",
          "fresh < " +
            humanSeconds(thresholds.fresh_within_seconds) +
            " · stale < " +
            humanSeconds(thresholds.stale_within_seconds) +
            " · outcome: " +
            (source.outcome || "unknown") +
            (typeof source.elapsed_seconds === "number" &&
            isFinite(source.elapsed_seconds)
              ? " · took " + source.elapsed_seconds.toFixed(2) + "s"
              : "")
        )
      );
      (source.notes || []).forEach(function (note) {
        right.appendChild(el("div", "health-when", note));
      });
      row.appendChild(right);

      container.appendChild(row);
    });

    var schedule = scheduleNote(meta);
    container.appendChild(
      live("div", "thresholds", function (t) {
        var states = sources.map(function (source) {
          return stateNow(source, t);
        });
        var tally = { fresh: 0, stale: 0, failed: 0, unknown: 0 };
        states.forEach(function (state) {
          if (has(tally, state)) tally[state] += 1;
          else tally.unknown += 1;
        });
        var parts = [
          tally.fresh + " fresh",
          tally.stale + " stale",
          tally.failed + " failed",
        ];
        // Only mentioned when non-zero, but never silently dropped from the
        // count -- rows that render must be accounted for in the total.
        if (tally.unknown) parts.push(tally.unknown + " unknown");
        return (
          "Overall: " +
          worstState(states) +
          " — " +
          parts.join(", ") +
          ". " +
          schedule +
          "The thresholds are constants derived from those figures, published " +
          "here so each state is checkable rather than asserted. States are " +
          "recomputed in your browser from the timestamps above, so they stay " +
          "truthful even if the pipeline stops."
        );
      })
    );
  }

  // ----------------------------------------------------------------- boot

  function wireThemeToggle() {
    var button = document.getElementById("theme-toggle");
    if (!button) return;
    button.addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme");
      var next;
      if (current === "dark") next = "light";
      else if (current === "light") next = "dark";
      else {
        next =
          window.matchMedia &&
          window.matchMedia("(prefers-color-scheme: dark)").matches
            ? "light"
            : "dark";
      }
      document.documentElement.setAttribute("data-theme", next);
      try {
        localStorage.setItem("orbital-watch-theme", next);
      } catch (e) {
        /* private mode: theme simply will not persist */
      }
    });
  }

  /* A full re-render replaces every panel, which would close an open
   * <details> and reset any horizontal scroll. A new document arrives every
   * ten minutes, so that would happen under the reader's hands. Note what
   * they had open and where they had scrolled to, and put it back. */
  function keepingReaderState(render) {
    var open = [];
    var scrolled = {};
    Array.prototype.forEach.call(document.querySelectorAll("details"), function (d) {
      var owner = d.closest("[id]");
      if (d.open && owner) open.push(owner.id);
    });
    Array.prototype.forEach.call(
      document.querySelectorAll(".table-scroll, .chart-wrap"),
      function (node) {
        var owner = node.closest("[id]");
        if (owner && node.scrollLeft) scrolled[owner.id] = node.scrollLeft;
      }
    );

    render();

    open.forEach(function (id) {
      var d = byId(id) && byId(id).querySelector("details");
      if (d) d.open = true;
    });
    Object.keys(scrolled).forEach(function (id) {
      var node = byId(id) && byId(id).querySelector(".table-scroll, .chart-wrap");
      if (node) node.scrollLeft = scrolled[id];
    });
  }

  function render() {
    var doc = shown.document;
    var meta = doc.meta || {};
    var now = clockNow();

    // Anything registered by a previous render points at DOM that is about to
    // be replaced. Drop it so closures cannot accumulate across renders.
    tickables.length = 0;

    var subEl = byId("site-sub");
    var observer = meta.observer;
    if (observer && subEl) {
      subEl.textContent =
        "Ground station: " +
        observer.name +
        " (" +
        num(observer.latitude_deg, 4) +
        "°, " +
        num(observer.longitude_deg, 4) +
        "°)";
    }

    var sourceEl = byId("document-source");
    sourceEl.textContent = sourceLine(now);
    onTick(function (t) {
      sourceEl.textContent = sourceLine(t);
    });

    var silenceEl = byId("staleness-banner");
    guard(silenceEl, "Document age check", function () {
      renderSilenceBanner(silenceEl, doc, now);
      onTick(function (t) {
        renderSilenceBanner(silenceEl, doc, t);
      });
    });

    var healthSources = Array.isArray(doc.health.sources) ? doc.health.sources : null;
    var overall = byId("overall-status");
    clear(overall);
    overall.appendChild(
      liveNode("span", null, function (t) {
        var summary = Core.systemSummary(
          (healthSources || []).map(function (source) {
            return stateNow(source, t);
          })
        );
        var key = has(STATUS_GLYPH, summary.worst) ? summary.worst : "unknown";
        var node = badge("status-" + key, STATUS_GLYPH[key], summary.label);
        if (summary.detail) node.appendChild(el("span", "status-detail", summary.detail));
        return node;
      })
    );

    // The assessment speaks about the present, so it is only shown as
    // written while the Kp source behind it is still current. That can
    // change while the page is open; when it does, the banner is redrawn.
    var correlationEl = byId("correlation");
    var kpSource = (healthSources || []).filter(function (source) {
      var named = (doc.space_weather.correlation || {}).source || "swpc_kp";
      return source && source.name === named;
    })[0];
    function kpStateAt(t) {
      return kpSource ? stateNow(kpSource, t) : null;
    }
    var drawnFor = kpStateAt(now);
    guard(correlationEl, "Operational assessment", function () {
      renderCorrelation(correlationEl, doc.space_weather, drawnFor);
    });
    onTick(function (t) {
      var state = kpStateAt(t);
      if ((state === "failed") === (drawnFor === "failed")) return;
      drawnFor = state;
      keepingReaderState(function () {
        renderCorrelation(correlationEl, doc.space_weather, state);
      });
    });
    var tilesEl = byId("weather-tiles");
    guard(tilesEl, "Space weather", function () {
      renderWeatherTiles(tilesEl, doc.space_weather, meta, doc.sky);
    });
    var chartEl = byId("kp-chart-card");
    chartEl.style.display = "";
    guard(chartEl, "Kp history chart", function () {
      renderKpChart(chartEl, doc.space_weather);
    });

    var skyEl = byId("sky");
    guard(skyEl, "Sky view", function () {
      renderSky(skyEl, doc.sky, meta);
    });

    var passesEl = byId("passes");
    guard(passesEl, "Pass predictions", function () {
      renderPasses(passesEl, doc.passes, meta, doc.generated_at, now);
    });

    var healthEl = byId("health");
    guard(healthEl, "System health", function () {
      renderHealth(healthEl, doc.health, meta);
    });

    var pipeline = meta.pipeline || {};
    byId("build-info").textContent =
      "Document generated " +
      utcStamp(doc.generated_at) +
      " UTC in " +
      num(pipeline.run_seconds, 2) +
      "s" +
      (pipeline.revision ? " by revision " + String(pipeline.revision).slice(0, 7) : "") +
      ". This page is static files: it reads one pre-generated document and " +
      "makes no upstream calls, so an upstream outage cannot take it down — it " +
      "only makes the data older, which the health panel reports.";
  }

  /** Nothing could be loaded at all: say so in every panel, with the reasons. */
  function renderLoadFailure(liveError, bakedError) {
    tickables.length = 0;
    var reason =
      "The live document could not be loaded (" +
      (liveError || "unknown error") +
      "), and neither could the copy shipped with this site (" +
      (bakedError || "unknown error") +
      ").";
    byId("document-source").textContent = "No data could be loaded. " + reason;
    clear(byId("staleness-banner"));
    clear(byId("overall-status"));
    byId("overall-status").appendChild(statusBadge("unknown"));

    showError(
      byId("correlation"),
      "Operational assessment unavailable",
      reason + " Without space weather no geomagnetic assessment can be made."
    );
    clear(byId("weather-tiles"));
    byId("kp-chart-card").style.display = "";
    showError(byId("kp-chart-card"), "Space weather data could not be loaded", reason);
    showError(byId("sky"), "Sky view could not be loaded", reason);
    showError(byId("passes"), "Pass data could not be loaded", reason);
    showError(
      byId("health"),
      "Health data could not be loaded",
      reason +
        " If you are opening this file directly from disk, the browser blocks " +
        "local reads — serve the directory over HTTP instead. This page will " +
        "keep trying once a minute."
    );
    byId("build-info").textContent =
      "No document loaded, so nothing is known about when the data was generated.";
  }

  var loading = false;

  /** Look for a document: on first load, and then once a minute. */
  function refresh() {
    if (loading) return;
    loading = true;
    Core.loadDocument(CONFIG, {
      fetch: window.fetch.bind(window),
      setTimeout: window.setTimeout.bind(window),
      clearTimeout: window.clearTimeout.bind(window),
      AbortController: window.AbortController,
    })
      .then(function (loaded) {
        noteServerTime(loaded.serverTimeMs);
        var next = Core.adoptLoad(shown, loaded);
        if (!next.document) {
          shown = null;
          renderLoadFailure(next.liveError, next.bakedError);
          return;
        }
        var first = !shown;
        shown = next;
        if (first) render();
        else if (next.changed) keepingReaderState(render);
        // Unchanged: the tick below picks up any change to refreshError.
        runTicks(clockNow());
      })
      .catch(function (error) {
        // Last line of defence. Nothing above should reject, but a blank page
        // with no explanation is the one outcome this project must not have.
        showError(
          byId("correlation"),
          "The dashboard failed to render",
          (error && error.message ? error.message : String(error)) +
            ". The raw data is still readable at " +
            (CONFIG.bakedDocumentUrl || "data/latest.json") +
            "."
        );
      })
      .then(function () {
        loading = false;
      });
  }

  function boot() {
    wireThemeToggle();
    refresh();
    window.setInterval(refresh, POLL_MS);
    // Refresh only the volatile parts. See RETICK_MS for why not everything.
    window.setInterval(function () {
      runTicks(clockNow());
    }, RETICK_MS);
    // A tab that was in the background has been throttled; catch up at once
    // instead of showing minutes-old positions until the next tick.
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) {
        runTicks(clockNow());
        refresh();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
