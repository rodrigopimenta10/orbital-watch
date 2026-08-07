/**
 * Orbital Watch refresh scheduler.
 *
 * Two jobs, both small on purpose — this Worker has no dependencies, so there
 * is nothing here to audit but the logic itself.
 *
 *   1. Trigger a Cloudflare Pages rebuild, which is what refreshes the data.
 *   2. Act as a dead-man's check on the *previous* cycle.
 *
 * Job 2 is the reason this Worker is worth having rather than a bare cron.
 * The failure that prompted all of this was not a build that broke loudly; it
 * was a scheduler that quietly never ran, where the only signal was a visitor
 * eventually noticing the staleness banner. Nothing watched the watchman.
 *
 * The check runs *before* the new build can possibly finish, so the health.json
 * it reads is the output of the previous cycle. That is exactly the question
 * worth asking: did the last refresh actually produce a fresh site? Checking
 * after our own trigger would only prove that a build we just started has not
 * finished yet.
 */

const ALERT_TITLE = "Orbital Watch: scheduled refresh appears to have stopped";

export default {
  async scheduled(controller, env, ctx) {
    // Trigger first. If the dead-man's check throws, the rebuild has already
    // been requested — diagnostics must never cost us the actual refresh.
    const triggered = await triggerRebuild(env);

    // Deliberately awaited rather than left to waitUntil: a scheduled Worker
    // that returns early can have its pending work cancelled, and a check that
    // silently does not run is precisely the failure mode being fixed.
    const checked = await checkPreviousCycle(env);

    console.log(JSON.stringify({ triggered, checked }));
  },
};

async function triggerRebuild(env) {
  if (!env.DEPLOY_HOOK) {
    console.error("DEPLOY_HOOK is not set; no rebuild was triggered");
    return { ok: false, reason: "missing DEPLOY_HOOK" };
  }

  try {
    const response = await fetch(env.DEPLOY_HOOK, { method: "POST" });
    if (!response.ok) {
      // Never log the response body: an error page from the hook endpoint can
      // echo the URL back, and that URL is a capability.
      console.error(`deploy hook returned HTTP ${response.status}`);
      return { ok: false, status: response.status };
    }
    return { ok: true, status: response.status };
  } catch (error) {
    console.error(`deploy hook request failed: ${error.name}`);
    return { ok: false, reason: error.name };
  }
}

/**
 * Synthetic check on every public endpoint: status AND content-type.
 *
 * A 200 alone proves nothing. Pages serves an SPA-style fallback for missing
 * assets, returning 200 with text/html — which is exactly how a blank page
 * once shipped while every naive check stayed green. Asserting the
 * content-type distinguishes "the endpoint exists" from "the fallback
 * swallowed a 404".
 */
const ENDPOINTS = [
  { path: "/", type: "text/html" },
  { path: "/data/health.json", type: "application/json" },
  { path: "/data/meta.json", type: "application/json" },
  { path: "/data/sky.json", type: "application/json" },
  { path: "/data/passes.json", type: "application/json" },
  { path: "/data/space_weather.json", type: "application/json" },
  { path: "/assets/app.js", type: "javascript" },
  { path: "/assets/theme.js", type: "javascript" },
  { path: "/assets/style.css", type: "text/css" },
  { path: "/assets/og.jpg", type: "image/jpeg" },
];

async function checkEndpoints(env) {
  const failures = [];
  for (const endpoint of ENDPOINTS) {
    try {
      const response = await fetch(env.SITE_URL + endpoint.path, {
        cf: { cacheTtl: 0 },
      });
      const contentType = response.headers.get("content-type") || "";
      if (!response.ok) {
        failures.push(`${endpoint.path}: HTTP ${response.status}`);
      } else if (!contentType.includes(endpoint.type)) {
        failures.push(
          `${endpoint.path}: expected ${endpoint.type}, got "${contentType}" ` +
            `(a 200 with the wrong type is the SPA fallback eating a 404)`,
        );
      }
    } catch (error) {
      failures.push(`${endpoint.path}: ${error.name}`);
    }
  }
  return failures;
}

async function checkPreviousCycle(env) {
  // Guard the coercion: Number("21,600") is NaN, and every comparison with
  // NaN is false -- a single typo in the var would silently disable the one
  // check whose whole purpose is noticing silence. Fall back loudly instead.
  let maxAge = Number(env.MAX_DATA_AGE_SECONDS);
  if (!Number.isFinite(maxAge) || maxAge <= 0) {
    if (env.MAX_DATA_AGE_SECONDS) {
      console.error(
        `MAX_DATA_AGE_SECONDS=${JSON.stringify(env.MAX_DATA_AGE_SECONDS)} ` +
          "is not a positive number; using the 21600s default",
      );
    }
    maxAge = 21600;
  }
  const url = `${env.SITE_URL}/data/health.json`;

  const endpointFailures = await checkEndpoints(env);
  if (endpointFailures.length) {
    return alert(
      env,
      `Synthetic check failed on ${endpointFailures.length} endpoint(s):\n` +
        endpointFailures.map((f) => `- ${f}`).join("\n"),
    );
  }

  let payload;
  try {
    const response = await fetch(url, { cf: { cacheTtl: 0 } });
    if (!response.ok) {
      return alert(env, `${url} returned HTTP ${response.status}.`);
    }
    payload = await response.json();
  } catch (error) {
    return alert(env, `Could not fetch ${url}: ${error.name}.`);
  }

  const generatedAt = Date.parse(payload && payload.generated_at);
  if (!Number.isFinite(generatedAt)) {
    return alert(env, `${url} has no parseable generated_at.`);
  }

  const ageSeconds = (Date.now() - generatedAt) / 1000;
  if (ageSeconds > maxAge) {
    return alert(
      env,
      `The deployed data is ${Math.round(ageSeconds / 3600)}h old ` +
        `(limit ${Math.round(maxAge / 3600)}h). The last scheduled rebuild ` +
        `either did not run or did not publish. Site: ${env.SITE_URL}`,
    );
  }

  return { ok: true, ageSeconds: Math.round(ageSeconds) };
}

/**
 * Report loudly. Opens a GitHub issue, reusing an existing open one rather
 * than filing a fresh issue every two hours for the same outage.
 *
 * Without GITHUB_TOKEN this still logs at error level, which surfaces in
 * `wrangler tail` and the dashboard — degraded, but never silent.
 */
async function alert(env, message) {
  console.error(`DEAD-MAN CHECK: ${message}`);

  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) {
    return { ok: false, alerted: "log-only", message };
  }

  const headers = {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "User-Agent": "orbital-watch-refresh-worker",
    "Content-Type": "application/json",
  };

  try {
    // Dedup via the plain issues list, not the search API: search has index
    // lag and is mid-migration to a new query grammar, and -- the actual bug
    // this replaces -- a failed dedup lookup must NOT fall through to
    // creation, or a single API hiccup during an outage files a duplicate
    // issue every two hours for as long as the outage lasts.
    const existing = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/issues?state=open&per_page=100`,
      { headers },
    );
    if (!existing.ok) {
      console.error(
        `issue dedup lookup failed (HTTP ${existing.status}); ` +
          "not filing, to avoid duplicates",
      );
      return { ok: false, alerted: "dedup-unavailable", message };
    }
    const open = await existing.json();
    if (Array.isArray(open) && open.some((i) => i && i.title === ALERT_TITLE)) {
      return { ok: false, alerted: "already-open", message };
    }

    const created = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/issues`,
      {
        method: "POST",
        headers,
        body: JSON.stringify({
          title: ALERT_TITLE,
          body:
            `${message}\n\n` +
            `Raised automatically by the refresh Worker's dead-man check.\n` +
            `Close this once a scheduled rebuild has published again.`,
        }),
      },
    );
    return { ok: false, alerted: created.ok ? "issue-opened" : "issue-failed", message };
  } catch (error) {
    console.error(`alerting failed: ${error.name}`);
    return { ok: false, alerted: "failed", message };
  }
}
