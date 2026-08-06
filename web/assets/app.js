/* Orbital Watch frontend.
 *
 * Loads the static JSON written by the build and renders it. There is no
 * upstream call here and no framework -- the page's only dependency is the
 * JSON files sitting next to it.
 *
 * Two rules govern everything below.
 *
 * 1. A panel whose data is missing says so, in place, with the reason. It
 *    never renders blank and never renders a number it cannot attribute.
 *    Every panel is rendered inside its own try/catch, so one bad field
 *    cannot take out the panels below it -- least of all the health panel,
 *    which is the one that would have explained the failure.
 *
 * 2. Freshness is recomputed here, from timestamps, rather than trusted from
 *    the build. See stateNow() for why that matters.
 */
(function () {
  "use strict";

  var DATA = "data/";

  /* If the page's own data is older than this, the scheduled rebuild has
   * probably stopped and we say so at the top of the page. Generous relative
   * to the hourly cadence so a single missed run is not alarming. */
  var REBUILD_ALARM_SECONDS = 3 * 3600;

  /* Relative times ("in 4m") drift on a tab left open. Re-render them. */
  var RETICK_MS = 30000;

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

  /** Render a panel so that a throw inside it cannot reach the other panels. */
  function guard(container, label, render) {
    try {
      render();
    } catch (error) {
      showError(
        container,
        label + " could not be rendered",
        (error && error.message ? error.message : String(error)) +
          ". The underlying JSON loaded, so this is a rendering fault rather " +
          "than a data outage."
      );
    }
  }

  /** Format a number, or "—" if it is missing or not finite. */
  function num(value, digits) {
    if (typeof value !== "number" || !isFinite(value)) return "—";
    return value.toFixed(digits === undefined ? 0 : digits);
  }

  /** Thousands-separated integer, locale-stable so it reads the same anywhere. */
  function int(value) {
    if (typeof value !== "number" || !isFinite(value)) return "—";
    return Math.round(value)
      .toString()
      .replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function parseTime(value) {
    if (!value) return null;
    var t = new Date(value);
    return isNaN(t.getTime()) ? null : t;
  }

  function humanDuration(seconds) {
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
    var t = parseTime(value);
    if (!t) return "unknown";
    return t.toISOString().replace("T", " ").slice(0, 19);
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
    var key = STATUS_GLYPH[state] ? state : "unknown";
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
    var spec = SEVERITY[severity] || SEVERITY.unknown;
    return badge(spec.cls, spec.glyph, spec.label);
  }

  // ------------------------------------------------------ freshness, live
  //
  // The build computes each source's state in Python and bakes it into
  // health.json. That value is correct at build time and decays afterwards:
  // if the scheduled workflow stops, the page keeps asserting "fresh" while
  // the timestamp beside it counts up into days. GitHub disables scheduled
  // workflows on repositories with no activity for 60 days, so for a project
  // that is finished and left alone this is close to inevitable.
  //
  // Everything needed to recompute the state client-side is already in the
  // JSON -- last_success plus the published thresholds -- so we do, and the
  // panel stays honest no matter how long the page outlives its build.

  function stateNow(source, now) {
    if (!source || !source.last_success) return "failed";
    var t = parseTime(source.last_success);
    if (!t) return "failed";

    var thresholds = source.thresholds || {};
    var fresh = thresholds.fresh_within_seconds;
    var stale = thresholds.stale_within_seconds;
    if (typeof fresh !== "number" || typeof stale !== "number") {
      return source.state || "unknown";
    }

    var age = (now - t) / 1000;
    if (age > stale) return "failed";
    if (age > fresh) return "stale";
    // Inside the freshness window by age -- but if we could not reach
    // upstream on the run that produced this page, it is not fresh.
    if (source.outcome === "cache_fallback") return "stale";
    return "fresh";
  }

  function worstState(states) {
    if (states.indexOf("failed") !== -1) return "failed";
    if (states.indexOf("stale") !== -1) return "stale";
    return states.length ? "fresh" : "unknown";
  }

  // ----------------------------------------------------- staleness banner

  function renderStalenessBanner(container, meta, now) {
    clear(container);
    if (!meta || !meta.generated_at) return;
    var built = parseTime(meta.generated_at);
    if (!built) return;

    var age = (now - built) / 1000;
    if (age <= REBUILD_ALARM_SECONDS) return;

    var banner = el("div", "banner banner-elevated");
    var heading = el("h3");
    heading.appendChild(badge("status-elevated", "◆", "stale build"));
    heading.appendChild(
      el("span", null, "This page was last rebuilt " + humanDuration(age) + " ago")
    );
    banner.appendChild(heading);
    banner.appendChild(
      el(
        "p",
        null,
        "The scheduled job rebuilds this site hourly, so a gap this long means " +
          "it has probably stopped running. Everything below is still exactly " +
          "what was true at " +
          utcStamp(meta.generated_at) +
          " UTC — it is simply that old. Source states are recomputed in your " +
          "browser from the published timestamps, so they degrade with age " +
          "rather than staying frozen at whatever the last build reported."
      )
    );
    container.appendChild(banner);
  }

  // ----------------------------------------------------------- correlation

  function renderCorrelation(container, payload) {
    clear(container);
    var c = payload.correlation;
    if (!c) {
      showError(
        container,
        "Assessment unavailable",
        "No correlation data in the build output."
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
    if (meta) card.appendChild(el("div", "meta", meta));
    if (provenance) card.appendChild(el("div", "provenance", provenance));
    return card;
  }

  function observedAt(time, now) {
    return time
      ? "NOAA SWPC · " + utcStamp(time) + " (" + relative(time, now) + ")"
      : "NOAA SWPC · no data this build";
  }

  function renderWeatherTiles(container, payload, meta, now) {
    clear(container);
    var w = payload.weather || {};

    if (typeof w.kp === "number") {
      container.appendChild(
        tile(
          "Planetary Kp",
          num(w.kp, 2),
          null,
          (w.kp_description || "") + " · trend " + (w.kp_trend || "unknown"),
          observedAt(w.kp_time, now)
        )
      );
    } else {
      container.appendChild(
        tile("Planetary Kp", "—", null, "Unavailable", observedAt(null, now))
      );
    }

    container.appendChild(
      tile(
        "Solar wind speed",
        num(w.solar_wind_speed_km_s, 0),
        "km/s",
        null,
        observedAt(w.solar_wind_time, now)
      )
    );

    container.appendChild(
      tile(
        "IMF Bz",
        num(w.bz_nt, 1),
        "nT",
        typeof w.bt_nt === "number" ? "Bt " + num(w.bt_nt, 1) + " nT" : null,
        observedAt(w.mag_time, now)
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
          : "NOAA SWPC · no data this build"
      )
    );

    var counts = (meta && meta.counts) || {};
    container.appendChild(
      tile(
        "Tracked objects",
        int(counts.tracked_satellites),
        null,
        typeof counts.above_horizon === "number"
          ? counts.above_horizon + " above horizon"
          : null,
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
      return p && typeof p.kp === "number" && isFinite(p.kp);
    });

    card.appendChild(el("div", "label", "Planetary Kp — recent history"));

    if (!history.length) {
      card.appendChild(el("p", "empty", "No Kp history available in this build."));
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
      title.textContent = utcStamp(point.time) + " — Kp " + num(kp, 2);
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
          " three-hourly observations. Bar height is the Kp value; colour " +
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

  function renderSky(container, payload, meta, now) {
    clear(container);
    var sats = (payload.satellites || []).filter(function (s) {
      return s && typeof s.elevation_deg === "number";
    });

    if (!sats.length) {
      container.appendChild(
        el(
          "p",
          "empty",
          "No tracked satellites above the horizon at the last build, or no " +
            "orbital data was available. Check system health below."
        )
      );
      return;
    }

    var built = buildTable(
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

    sats.forEach(function (s) {
      var row = el("tr");
      row.appendChild(el("td", "name", s.name || "unknown"));
      row.appendChild(groupTag(s.group || "—"));
      row.appendChild(el("td", "num", num(s.elevation_deg, 1) + "°"));
      row.appendChild(el("td", "num", num(s.azimuth_deg, 0) + "°"));
      row.appendChild(el("td", "num dim", s.compass || "—"));
      row.appendChild(el("td", "num", int(s.range_km) + " km"));
      row.appendChild(el("td", "num", int(s.altitude_km) + " km"));
      row.appendChild(el("td", "num dim", num(s.tle_age_hours, 1) + " h"));
      built.tbody.appendChild(row);
    });

    container.appendChild(built.scroll);
    container.appendChild(
      el(
        "div",
        "provenance",
        'Positions computed with SGP4 from Celestrak element sets. "Elem. age" ' +
          "is the age of the element set at build time — propagation error " +
          "grows with it."
      )
    );
    var note = coverageNote(meta);
    if (note) container.appendChild(el("div", "provenance", note));
    if (now) {
      container.appendChild(
        el(
          "div",
          "provenance",
          "Positions are a snapshot at " +
            utcStamp(payload.generated_at) +
            " UTC (" +
            relative(payload.generated_at, now) +
            "), not live."
        )
      );
    }
  }

  function renderPasses(container, payload, meta, now) {
    clear(container);
    var all = (payload.passes || []).filter(function (p) {
      return p && p.start;
    });

    // passes.json is generated at build time and the page may be viewed an
    // hour or more later, so part of the schedule is already history. Showing
    // "18m ago" under a column headed "In" reads as a bug even though the
    // data is fine.
    var upcoming = [];
    var elapsed = 0;
    all.forEach(function (p) {
      var end = parseTime(p.end);
      if (end && end < now) elapsed += 1;
      else upcoming.push(p);
    });

    if (!upcoming.length) {
      container.appendChild(
        el(
          "p",
          "empty",
          all.length
            ? "All " +
              all.length +
              " predicted passes in this build have already elapsed. The page " +
              "is waiting on the next scheduled rebuild."
            : "No passes predicted, or no orbital data was available. Check " +
              "system health below."
        )
      );
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
      row.appendChild(el("td", "num dim", relative(p.start, now)));
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
          " h ahead from the last build at " +
          utcStamp(payload.generated_at) +
          " UTC." +
          (elapsed
            ? " " + elapsed + " pass(es) from this build have already elapsed " +
              "and are hidden."
            : "")
      )
    );
    var note = coverageNote(meta);
    if (note) container.appendChild(el("div", "provenance", note));
  }

  // --------------------------------------------------------------- health

  function renderHealth(container, payload, now) {
    clear(container);
    var sources = payload.sources || [];

    if (!sources.length) {
      showError(
        container,
        "Health unavailable",
        "The build produced no source health records."
      );
      return;
    }

    var counts = { fresh: 0, stale: 0, failed: 0 };

    sources.forEach(function (source) {
      var live = stateNow(source, now);
      if (counts[live] === undefined) counts[live] = 0;
      counts[live] += 1;

      var row = el("div", "health-row");

      var left = el("div");
      left.appendChild(el("div", "health-name", source.label || source.name));
      left.appendChild(el("div", "health-url", source.url || ""));
      row.appendChild(left);

      var middle = el("div");
      middle.appendChild(statusBadge(live));
      middle.appendChild(
        el(
          "div",
          "health-when",
          source.last_success ? relative(source.last_success, now) : "never succeeded"
        )
      );
      row.appendChild(middle);

      var right = el("div");
      right.appendChild(el("div", "health-detail", source.detail || ""));

      // If age has pushed the state past what the build recorded, say so
      // rather than silently disagreeing with the detail text above.
      if (source.state && live !== source.state) {
        right.appendChild(
          el(
            "div",
            "health-when",
            "Recomputed in-browser: was " +
              source.state +
              " at build time, now " +
              live +
              " given the elapsed time."
          )
        );
      }

      if (source.last_success) {
        right.appendChild(
          el(
            "div",
            "health-when",
            "Last success " + utcStamp(source.last_success) + " UTC"
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
            (typeof source.elapsed_seconds === "number"
              ? " · fetched in " + source.elapsed_seconds.toFixed(2) + "s"
              : "")
        )
      );
      (source.notes || []).forEach(function (note) {
        right.appendChild(el("div", "health-when", note));
      });
      row.appendChild(right);

      container.appendChild(row);
    });

    container.appendChild(
      el(
        "div",
        "thresholds",
        "Overall: " +
          worstState(
            sources.map(function (s) {
              return stateNow(s, now);
            })
          ) +
          " — " +
          counts.fresh +
          " fresh, " +
          counts.stale +
          " stale, " +
          counts.failed +
          " failed. Thresholds are build-time constants, published here so the " +
          "state is checkable rather than asserted. States are recomputed in " +
          "your browser from the timestamps above, so they stay truthful even " +
          "if the scheduled rebuild stops."
      )
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

  function render(results) {
    var meta = results[0];
    var weather = results[1];
    var sky = results[2];
    var passes = results[3];
    var health = results[4];
    var now = new Date();

    var metaData = meta.ok ? meta.data : null;

    if (metaData && metaData.observer) {
      var o = metaData.observer;
      document.getElementById("site-sub").textContent =
        "Ground station: " +
        o.name +
        " (" +
        num(o.latitude_deg, 4) +
        "°, " +
        num(o.longitude_deg, 4) +
        "°) · built " +
        utcStamp(metaData.generated_at) +
        " UTC (" +
        relative(metaData.generated_at, now) +
        ")";

      document.getElementById("build-info").textContent =
        "Data generated " +
        utcStamp(metaData.generated_at) +
        " UTC (" +
        relative(metaData.generated_at, now) +
        ") in " +
        num(metaData.build_seconds, 2) +
        "s. This page is static: it reads pre-generated JSON and makes no " +
        "upstream calls, so an upstream outage cannot take it down — it only " +
        "makes the data below older, which the health panel reports.";
    } else if (!meta.ok) {
      document.getElementById("build-info").textContent =
        "Build metadata could not be loaded (" +
        meta.error +
        "), so the generation time is unknown.";
    }

    guard(document.getElementById("staleness-banner"), "Staleness check", function () {
      renderStalenessBanner(
        document.getElementById("staleness-banner"),
        metaData,
        now
      );
    });

    var overall = document.getElementById("overall-status");
    clear(overall);
    overall.appendChild(
      statusBadge(
        health.ok
          ? worstState(
              (health.data.sources || []).map(function (s) {
                return stateNow(s, now);
              })
            )
          : "unknown"
      )
    );

    var correlationEl = document.getElementById("correlation");
    var tilesEl = document.getElementById("weather-tiles");
    var chartEl = document.getElementById("kp-chart-card");
    if (weather.ok) {
      chartEl.style.display = "";
      guard(correlationEl, "Operational assessment", function () {
        renderCorrelation(correlationEl, weather.data);
      });
      guard(tilesEl, "Space weather", function () {
        renderWeatherTiles(tilesEl, weather.data, metaData, now);
      });
      guard(chartEl, "Kp history chart", function () {
        renderKpChart(chartEl, weather.data);
      });
    } else {
      showError(
        correlationEl,
        "Space weather data could not be loaded",
        weather.error + ". The build may not have run, or the file is missing."
      );
      clear(tilesEl);
      clear(chartEl);
      chartEl.style.display = "none";
    }

    var skyEl = document.getElementById("sky");
    if (sky.ok) {
      guard(skyEl, "Sky view", function () {
        renderSky(skyEl, sky.data, metaData, now);
      });
    } else {
      showError(skyEl, "Sky view could not be loaded", sky.error);
    }

    var passesEl = document.getElementById("passes");
    if (passes.ok) {
      guard(passesEl, "Pass predictions", function () {
        renderPasses(passesEl, passes.data, metaData, now);
      });
    } else {
      showError(passesEl, "Pass data could not be loaded", passes.error);
    }

    var healthEl = document.getElementById("health");
    if (health.ok) {
      guard(healthEl, "System health", function () {
        renderHealth(healthEl, health.data, now);
      });
    } else {
      showError(
        healthEl,
        "Health data could not be loaded",
        health.error +
          ". If you are opening this file directly from disk, the browser " +
          "blocks local JSON reads — serve the directory over HTTP instead."
      );
    }
  }

  function boot() {
    wireThemeToggle();

    Promise.all([
      loadJSON("meta"),
      loadJSON("space_weather"),
      loadJSON("sky"),
      loadJSON("passes"),
      loadJSON("health"),
    ])
      .then(function (results) {
        render(results);
        // Relative times and recomputed states drift on a tab left open.
        window.setInterval(function () {
          try {
            render(results);
          } catch (e) {
            /* a re-render fault must not kill the already-rendered page */
          }
        }, RETICK_MS);
      })
      .catch(function (error) {
        // Last line of defence. Nothing above should reject, but a blank page
        // with no explanation is the one outcome this project must not have.
        var container = document.getElementById("correlation");
        if (container) {
          showError(
            container,
            "The dashboard failed to render",
            (error && error.message ? error.message : String(error)) +
              ". The raw data is still readable at data/health.json."
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
