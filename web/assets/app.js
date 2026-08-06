/* Orbital Watch frontend.
 *
 * Loads the static JSON written by the build and renders it. There is no
 * upstream call here and no framework -- the page's only dependency is four
 * JSON files sitting next to it.
 *
 * Rendering rule throughout: a panel whose data is missing says so, in place,
 * with the reason. It never renders blank and never renders a number it
 * cannot attribute.
 */
(function () {
  "use strict";

  var DATA = "data/";

  // ---------------------------------------------------------------- utils

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  /** Fetch JSON, resolving to {ok, data, error} rather than throwing. */
  function loadJSON(name) {
    return fetch(DATA + name + ".json", { cache: "no-cache" })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("HTTP " + response.status + " " + response.statusText);
        }
        return response.json();
      })
      .then(function (data) {
        return { ok: true, data: data, error: null };
      })
      .catch(function (error) {
        return { ok: false, data: null, error: error.message || String(error) };
      });
  }

  function showError(container, title, detail) {
    clear(container);
    var box = el("div", "banner error-box");
    box.appendChild(el("h3", null, title));
    box.appendChild(el("p", null, detail));
    container.appendChild(box);
  }

  function parseTime(value) {
    if (!value) return null;
    var t = new Date(value);
    return isNaN(t.getTime()) ? null : t;
  }

  /** "3h 12m ago" / "in 4m" -- relative phrasing an operator reads quickly. */
  function relative(value, now) {
    var t = parseTime(value);
    if (!t) return "unknown";
    var seconds = Math.round(((now || new Date()) - t) / 1000);
    var future = seconds < 0;
    var s = Math.abs(seconds);
    var text;
    if (s < 60) text = s + "s";
    else if (s < 3600) text = Math.floor(s / 60) + "m";
    else if (s < 86400) {
      var h = Math.floor(s / 3600);
      var m = Math.floor((s % 3600) / 60);
      text = m ? h + "h " + m + "m" : h + "h";
    } else {
      var d = Math.floor(s / 86400);
      var hh = Math.floor((s % 86400) / 3600);
      text = hh ? d + "d " + hh + "h" : d + "d";
    }
    return future ? "in " + text : text + " ago";
  }

  /** "2026-08-06 15:21:12" -- always trims sub-second precision, which is
   *  noise here and left a stray "." when milliseconds were non-zero. */
  function utcStamp(value) {
    var t = parseTime(value);
    if (!t) return "unknown";
    return t.toISOString().replace("T", " ").slice(0, 19);
  }

  function humanSeconds(seconds) {
    if (seconds === null || seconds === undefined) return "unknown";
    if (seconds < 60) return Math.round(seconds) + "s";
    if (seconds < 3600) return Math.round(seconds / 60) + "m";
    if (seconds < 86400) return Math.round(seconds / 3600) + "h";
    return Math.round(seconds / 86400) + "d";
  }

  /** Status glyphs double the colour cue with a shape, for greyscale and CVD. */
  var STATUS_GLYPH = {
    fresh: "●", // filled circle
    stale: "◐", // half-filled circle
    failed: "▲", // triangle
    unknown: "○", // hollow circle
  };

  function statusBadge(state) {
    var key = STATUS_GLYPH[state] ? state : "unknown";
    var span = el("span", "status status-" + key);
    span.appendChild(el("span", "glyph", STATUS_GLYPH[key]));
    span.appendChild(el("span", null, key));
    return span;
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
    var spec = SEVERITY[severity] || SEVERITY.unknown;
    var span = el("span", "status " + spec.cls);
    span.appendChild(el("span", "glyph", spec.glyph));
    span.appendChild(el("span", null, spec.label));
    return span;
  }

  // ----------------------------------------------------------- correlation

  function renderCorrelation(container, payload) {
    clear(container);
    var c = payload.correlation;
    if (!c) {
      showError(container, "Assessment unavailable", "No correlation data in the build output.");
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
      var why = el("details");
      why.style.marginTop = "0.7rem";
      var summary = el("summary", null, "Why this matters");
      summary.style.cursor = "pointer";
      summary.style.fontSize = "0.85rem";
      summary.style.color = "var(--text-secondary)";
      why.appendChild(summary);
      var para = el("p", null, c.explanation);
      para.style.marginTop = "0.5rem";
      why.appendChild(para);
      banner.appendChild(why);
    }

    container.appendChild(banner);
  }

  // -------------------------------------------------------------- weather

  function tile(label, value, unit, meta, provenance) {
    var card = el("div", "card tile");
    card.appendChild(el("div", "label", label));
    var v = el("div", "value", value);
    if (unit) {
      var u = el("span", "unit", unit);
      v.appendChild(u);
    }
    card.appendChild(v);
    if (meta) card.appendChild(el("div", "meta", meta));
    if (provenance) card.appendChild(el("div", "provenance", provenance));
    return card;
  }

  function renderWeatherTiles(container, payload, meta) {
    clear(container);
    var w = payload.weather || {};

    if (w.kp !== null && w.kp !== undefined) {
      container.appendChild(
        tile(
          "Planetary Kp",
          w.kp,
          null,
          (w.kp_description || "") + " · trend " + (w.kp_trend || "unknown"),
          "NOAA SWPC · observed " + utcStamp(w.kp_time) + " (" + relative(w.kp_time) + ")"
        )
      );
    } else {
      container.appendChild(
        tile("Planetary Kp", "—", null, "Unavailable", "NOAA SWPC · no data this build")
      );
    }

    container.appendChild(
      tile(
        "Solar wind speed",
        w.solar_wind_speed_km_s !== null && w.solar_wind_speed_km_s !== undefined
          ? w.solar_wind_speed_km_s
          : "—",
        "km/s",
        null,
        w.solar_wind_time
          ? "NOAA SWPC · " + utcStamp(w.solar_wind_time) + " (" + relative(w.solar_wind_time) + ")"
          : "NOAA SWPC · no data this build"
      )
    );

    container.appendChild(
      tile(
        "IMF Bz",
        w.bz_nt !== null && w.bz_nt !== undefined ? w.bz_nt : "—",
        "nT",
        w.bt_nt !== null && w.bt_nt !== undefined ? "Bt " + w.bt_nt + " nT" : null,
        w.mag_time
          ? "NOAA SWPC · " + utcStamp(w.mag_time) + " (" + relative(w.mag_time) + ")"
          : "NOAA SWPC · no data this build"
      )
    );

    var tracked = meta && meta.counts ? meta.counts.tracked_satellites : null;
    var above = meta && meta.counts ? meta.counts.above_horizon : null;
    container.appendChild(
      tile(
        "Tracked objects",
        tracked === null || tracked === undefined ? "—" : tracked,
        null,
        above === null || above === undefined ? null : above + " above horizon",
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
    if (kp >= 7) return "var(--status-critical)";
    if (kp >= 5) return "var(--status-serious)";
    if (kp >= 4) return "var(--status-warning)";
    return "var(--status-good)";
  }

  function renderKpChart(card, payload) {
    clear(card);
    var w = payload.weather || {};
    var history = w.kp_history || [];

    card.appendChild(el("div", "label", "Planetary Kp — recent history"));

    if (!history.length) {
      var note = el("p", "empty", "No Kp history available in this build.");
      card.appendChild(note);
      return;
    }

    var width = Math.max(640, Math.min(1100, history.length * 16));
    var height = 220;
    var pad = { top: 16, right: 16, bottom: 34, left: 34 };
    var plotW = width - pad.left - pad.right;
    var plotH = height - pad.top - pad.bottom;
    var maxKp = 9;

    var wrap = el("div", "chart-wrap");
    var svg = svgEl("svg", {
      width: width,
      height: height,
      viewBox: "0 0 " + width + " " + height,
      role: "img",
      "aria-label":
        "Bar chart of planetary Kp index over the recent period. Latest value " +
        w.kp +
        ", " +
        (w.kp_label || "") + ".",
    });

    function y(kp) {
      return pad.top + plotH - (kp / maxKp) * plotH;
    }

    // Gridlines + y ticks, recessive.
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

    // Bars. 2px surface gap between neighbours, 2px rounded data-end.
    var slot = plotW / history.length;
    var barW = Math.max(3, slot - 2);

    history.forEach(function (point, index) {
      var kp = point.kp;
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
      title.textContent = utcStamp(point.time) + " — Kp " + kp;
      rect.appendChild(title);
      svg.appendChild(rect);
    });

    // Storm threshold, labelled. This is what makes the colour redundant
    // rather than load-bearing.
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
    // Left-anchored: the newest (and during a storm, tallest) bars sit at the
    // right-hand end, and a right-anchored label collides with them exactly
    // when the threshold matters most.
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

    // Baseline.
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

    // Time bounds only -- a label per bar would be noise.
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
      last.textContent = utcStamp(history[history.length - 1].time).slice(0, 16) + " UTC";
      svg.appendChild(last);
    }

    wrap.appendChild(svg);
    card.appendChild(wrap);
    card.appendChild(
      el(
        "div",
        "chart-caption",
        history.length +
          " three-hourly observations. Bar height is the Kp value; colour restates the NOAA severity band. Hover a bar for its timestamp."
      )
    );
  }

  // ------------------------------------------------------------- sky view

  function renderSky(container, payload) {
    clear(container);
    var sats = payload.satellites || [];

    if (!sats.length) {
      container.appendChild(
        el(
          "p",
          "empty",
          "No tracked satellites above the horizon at the last build, or no orbital data was available. Check system health below."
        )
      );
      return;
    }

    var columns = [
      ["Satellite", "name"],
      ["Group", "group"],
      ["Elev", "num"],
      ["Az", "num"],
      ["Dir", "num"],
      ["Range", "num"],
      ["Alt", "num"],
      ["Elem. age", "num"],
    ];

    var scroll = el("div", "table-scroll");
    var table = el("table");
    var thead = el("thead");
    var headRow = el("tr");
    columns.forEach(function (col) {
      var th = el("th", col[1] === "num" ? "num" : null, col[0]);
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = el("tbody");
    sats.forEach(function (s) {
      var row = el("tr");
      row.appendChild(el("td", "name", s.name));
      var groupCell = el("td");
      groupCell.appendChild(el("span", "group-tag", s.group));
      row.appendChild(groupCell);
      row.appendChild(el("td", "num", s.elevation_deg.toFixed(1) + "°"));
      row.appendChild(el("td", "num", s.azimuth_deg.toFixed(0) + "°"));
      row.appendChild(el("td", "num dim", s.compass));
      row.appendChild(el("td", "num", Math.round(s.range_km).toLocaleString() + " km"));
      row.appendChild(el("td", "num", Math.round(s.altitude_km).toLocaleString() + " km"));
      row.appendChild(el("td", "num dim", s.tle_age_hours + " h"));
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    scroll.appendChild(table);
    container.appendChild(scroll);

    container.appendChild(
      el(
        "div",
        "provenance",
        "Positions computed with SGP4 from Celestrak element sets. " +
          '"Elem. age" is the age of the element set at build time — propagation error grows with it.'
      )
    );
  }

  // --------------------------------------------------------------- passes

  function renderPasses(container, payload) {
    clear(container);
    var passes = payload.passes || [];

    if (!passes.length) {
      container.appendChild(
        el(
          "p",
          "empty",
          "No passes predicted, or no orbital data was available. Check system health below."
        )
      );
      return;
    }

    var now = new Date();
    var scroll = el("div", "table-scroll");
    var table = el("table");
    var thead = el("thead");
    var headRow = el("tr");
    [
      ["Satellite", false],
      ["Group", false],
      ["Start (UTC)", false],
      ["In", true],
      ["Peak elev", true],
      ["Duration", true],
      ["Track", false],
    ].forEach(function (col) {
      headRow.appendChild(el("th", col[1] ? "num" : null, col[0]));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = el("tbody");
    passes.forEach(function (p) {
      var row = el("tr");
      row.appendChild(el("td", "name", p.name));
      var groupCell = el("td");
      groupCell.appendChild(el("span", "group-tag", p.group));
      row.appendChild(groupCell);
      row.appendChild(el("td", null, utcStamp(p.start).slice(0, 16)));
      row.appendChild(el("td", "num dim", relative(p.start, now)));
      row.appendChild(el("td", "num", p.peak_elevation_deg.toFixed(0) + "°"));
      row.appendChild(el("td", "num", p.duration_minutes.toFixed(1) + " min"));
      row.appendChild(
        el("td", "dim", p.start_compass + " → " + p.peak_compass + " → " + p.end_compass)
      );
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    scroll.appendChild(table);
    container.appendChild(scroll);

    container.appendChild(
      el(
        "div",
        "provenance",
        "Minimum elevation " +
          (payload.min_elevation_deg !== undefined ? payload.min_elevation_deg : "?") +
          "°, searched " +
          (payload.search_hours !== undefined ? payload.search_hours : "?") +
          " h ahead from the last build at " +
          utcStamp(payload.generated_at) +
          " UTC."
      )
    );
  }

  // --------------------------------------------------------------- health

  function renderHealth(container, payload) {
    clear(container);
    var sources = payload.sources || [];

    if (!sources.length) {
      showError(container, "Health unavailable", "The build produced no source health records.");
      return;
    }

    var now = new Date();
    sources.forEach(function (source) {
      var row = el("div", "health-row");

      var left = el("div");
      left.appendChild(el("div", "health-name", source.label || source.name));
      left.appendChild(el("div", "health-url", source.url || ""));
      row.appendChild(left);

      var middle = el("div");
      middle.appendChild(statusBadge(source.state));
      var when = el(
        "div",
        "health-when",
        source.last_success
          ? relative(source.last_success, now)
          : "never succeeded"
      );
      middle.appendChild(when);
      row.appendChild(middle);

      var right = el("div");
      right.appendChild(el("div", "health-detail", source.detail || ""));
      if (source.last_success) {
        right.appendChild(
          el("div", "health-when", "Last success " + utcStamp(source.last_success) + " UTC")
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
            (source.outcome || "unknown")
        )
      );
      row.appendChild(right);

      container.appendChild(row);
    });

    var counts = payload.counts || {};
    container.appendChild(
      el(
        "div",
        "thresholds",
        "Overall: " +
          (payload.overall || "unknown") +
          " — " +
          (counts.fresh || 0) +
          " fresh, " +
          (counts.stale || 0) +
          " stale, " +
          (counts.failed || 0) +
          " failed. Thresholds are build-time constants, shown here so the state is checkable rather than asserted."
      )
    );
  }

  // ----------------------------------------------------------------- boot

  function applyStoredTheme() {
    var stored = null;
    try {
      stored = localStorage.getItem("orbital-watch-theme");
    } catch (e) {
      /* private mode; fall back to OS preference */
    }
    if (stored === "light" || stored === "dark") {
      document.documentElement.setAttribute("data-theme", stored);
    }
  }

  function wireThemeToggle() {
    var button = document.getElementById("theme-toggle");
    if (!button) return;
    button.addEventListener("click", function () {
      var current = document.documentElement.getAttribute("data-theme");
      var next;
      if (current === "dark") next = "light";
      else if (current === "light") next = "dark";
      else {
        next = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
          ? "light"
          : "dark";
      }
      document.documentElement.setAttribute("data-theme", next);
      try {
        localStorage.setItem("orbital-watch-theme", next);
      } catch (e) {
        /* ignore */
      }
    });
  }

  function boot() {
    applyStoredTheme();
    wireThemeToggle();

    Promise.all([
      loadJSON("meta"),
      loadJSON("space_weather"),
      loadJSON("sky"),
      loadJSON("passes"),
      loadJSON("health"),
    ]).then(function (results) {
      var meta = results[0];
      var weather = results[1];
      var sky = results[2];
      var passes = results[3];
      var health = results[4];

      // Header: observer and build time.
      if (meta.ok && meta.data.observer) {
        var o = meta.data.observer;
        var sub = document.getElementById("site-sub");
        sub.textContent =
          "Ground station: " +
          o.name +
          " (" +
          o.latitude_deg.toFixed(4) +
          "°, " +
          o.longitude_deg.toFixed(4) +
          "°) · built " +
          utcStamp(meta.data.generated_at) +
          " UTC (" +
          relative(meta.data.generated_at) +
          ")";

        document.getElementById("build-info").textContent =
          "Data generated " +
          utcStamp(meta.data.generated_at) +
          " UTC (" +
          relative(meta.data.generated_at) +
          ") in " +
          meta.data.build_seconds +
          "s. This page is static: it reads pre-generated JSON and makes no upstream calls, so an upstream outage cannot take it down — it only makes the data below older, which the health panel reports.";
      }

      // Overall badge in the masthead.
      var overall = document.getElementById("overall-status");
      clear(overall);
      if (health.ok) {
        overall.appendChild(statusBadge(health.data.overall));
      } else {
        overall.appendChild(statusBadge("unknown"));
      }

      if (weather.ok) {
        renderCorrelation(document.getElementById("correlation"), weather.data);
        renderWeatherTiles(
          document.getElementById("weather-tiles"),
          weather.data,
          meta.ok ? meta.data : null
        );
        renderKpChart(document.getElementById("kp-chart-card"), weather.data);
      } else {
        showError(
          document.getElementById("correlation"),
          "Space weather data could not be loaded",
          weather.error + ". The build may not have run, or the file is missing."
        );
        // Nothing to draw -- collapse the containers rather than leaving an
        // empty card floating on the page.
        var tiles = document.getElementById("weather-tiles");
        var chartCard = document.getElementById("kp-chart-card");
        clear(tiles);
        clear(chartCard);
        chartCard.style.display = "none";
      }

      if (sky.ok) {
        renderSky(document.getElementById("sky"), sky.data);
      } else {
        showError(document.getElementById("sky"), "Sky view could not be loaded", sky.error);
      }

      if (passes.ok) {
        renderPasses(document.getElementById("passes"), passes.data);
      } else {
        showError(document.getElementById("passes"), "Pass data could not be loaded", passes.error);
      }

      if (health.ok) {
        renderHealth(document.getElementById("health"), health.data);
      } else {
        showError(
          document.getElementById("health"),
          "Health data could not be loaded",
          health.error +
            ". If you are opening this file directly from disk, the browser blocks local JSON reads — serve the directory over HTTP instead."
        );
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
