/* Bibb voter turnout dashboard.
 *
 * Single fetch direct from the R2 public bucket. No proxy, no worker on
 * the read path — the bucket needs Access-Control-Allow-Origin set so the
 * browser allows the cross-origin GET. Data is public, so a wildcard
 * origin is fine.
 *
 * For local preview: drop a turnout.json next to index.html and switch
 * DATA_URL to "./turnout.json" temporarily.
 */

// Production reads R2 directly. Local preview uses ?preview=1 with a
// hand-built site/turnout.json (gitignored) for design-time testing.
const DATA_URL = new URLSearchParams(location.search).has("preview")
  ? "./turnout.json"
  : "https://votetally.kerryhatcher.com/turnout.json";

const fmt = new Intl.NumberFormat("en-US");

const ELECTION_NAME_OVERRIDE = {
  // Friendly names for known elections, since the source CSV only has the type.
  "A-12599": "May 19, 2026 General Primary",
};

// Read the active palette from CSS custom properties. Single source of truth
// is styles.css; this function just observes it. Re-read on every chart build
// so a theme switch (light ↔ dark) picks up the new tokens automatically.
function theme() {
  const cs = getComputedStyle(document.documentElement);
  const get = (name, fallback) => (cs.getPropertyValue(name).trim() || fallback);
  const accent = get("--accent", "#a32638");
  return {
    accent,
    ink: get("--ink", "#1a1a1a"),
    inkSoft: get("--ink-soft", "#555555"),
    grid: get("--chart-grid", "rgba(0,0,0,0.05)"),
    tick: get("--chart-tick", "#555555"),
    ring: get("--chart-ring", "#ffffff"),
    empty: get("--chart-empty", "rgba(0,0,0,0.06)"),
    // Political colors are not theme tokens (Democrat-blue / Republican-red
    // are signifiers that must read the same in both modes). Republican
    // tracks --accent so it stays consistent with the brand on either canvas.
    party: {
      DEMOCRAT: "#3b82f6",
      REPUBLICAN: accent,
      "NON-PARTISAN": "#8a8888",
    },
    // Generic categorical palette for non-political breakdowns. Tuned so
    // every entry has ≥3:1 against both light and dark canvases.
    palette: [accent, "#3b82f6", "#0ea5a4", "#9333ea", "#f59e0b", "#8a8888"],
  };
}

// Chart registry: hold instances so we can destroy + rebuild on theme change.
const chartRegistry = new Map();

function registerChart(canvasId, chart) {
  const prev = chartRegistry.get(canvasId);
  if (prev) prev.destroy();
  chartRegistry.set(canvasId, chart);
}

// Canvas content is invisible to assistive tech. Promote each chart to an
// accessible image with a one-line summary and a sibling sr-only paragraph
// containing the full series. Promotes the canvas to role=img so SR users
// hear the summary instead of "graphic".
function describeCanvas(canvasId, summary, fullText) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return;
  ctx.setAttribute("role", "img");
  ctx.setAttribute("aria-label", summary);
  const wrap = ctx.parentElement;
  if (!wrap) return;
  let sr = wrap.querySelector(":scope > .visually-hidden");
  if (!sr) {
    sr = document.createElement("p");
    sr.className = "visually-hidden";
    wrap.appendChild(sr);
  }
  sr.textContent = fullText || summary;
}

function electionDisplay(election) {
  if (!election || !election.id) return "";
  return ELECTION_NAME_OVERRIDE[election.id] ||
    `${election.type} · ${election.date}`;
}

function formatTimestamp(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, {
    weekday: "short", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit", timeZoneName: "short",
  });
}

// Cron runs 4×/day (09:30/15:30/19:30/21:30 EDT). Worst expected gap is the
// overnight ~12h window. Past ~14h we're outside the schedule (italic dek);
// past ~24h is almost certainly a cron failure (also desaturate the headline).
function freshnessState(iso) {
  if (!iso) return "unknown";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "unknown";
  const ageHours = (Date.now() - d.getTime()) / 3_600_000;
  if (ageHours > 24) return "stale";
  if (ageHours > 14) return "overnight";
  return "fresh";
}

function relativeAge(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const ageHours = (Date.now() - d.getTime()) / 3_600_000;
  if (ageHours < 1) return "just now";
  if (ageHours < 24) return `${Math.round(ageHours)}h ago`;
  return `${Math.round(ageHours / 24)}d ago`;
}

function renderHeadline(data) {
  const cur = data.current || {};
  const hub = data.hub || {};
  const election = data.election || {};

  // Hub is the preferred headline source — its turnout is hours-fresh while
  // the file lags 1–2 days. Fall back to the file's `current.total` when
  // hub data isn't present yet (e.g., before the first `votetally hub` run).
  const headlineTotal = (typeof hub.turnout === "number")
    ? hub.turnout
    : (cur.total || 0);
  const headlineFreshness = hub.data_as_of || cur.scraped_at;

  document.getElementById("kicker").textContent =
    `Bibb County · ${electionDisplay(election)}`;

  const totalEl = document.getElementById("total");
  totalEl.textContent = fmt.format(headlineTotal);

  // Fold turnout rate + active voter base into the subtitle when the hub
  // captured them. Editorial dek pattern, not a KPI tile — keeps the headline
  // numeral primary while contextualizing what fraction it represents.
  if (typeof hub.turnout_pct === "number" && typeof hub.active_voters === "number") {
    document.getElementById("subtitle").textContent =
      `ballots cast · ${hub.turnout_pct}% of ${fmt.format(hub.active_voters)} active voters`;
  }

  // Staleness tiering uses data.updated_at (the JSON regen heartbeat) — that
  // tracks the cron itself rather than the upstream "as of" string, which is
  // pre-formatted by the scraper and not always parseable.
  const heartbeat = data.updated_at || cur.scraped_at;
  const state = freshnessState(heartbeat);
  const updatedEl = document.getElementById("updated");

  totalEl.classList.toggle("is-stale", state === "stale");
  updatedEl.classList.toggle("is-overnight", state === "overnight");
  updatedEl.classList.toggle("is-stale", state === "stale");

  if (state === "stale") {
    updatedEl.textContent = `Last updated ${relativeAge(heartbeat)}. Refresh may be delayed.`;
  } else if (state === "overnight") {
    updatedEl.textContent = `Last updated ${relativeAge(heartbeat)}`;
  } else if (headlineFreshness) {
    updatedEl.textContent = hub.data_as_of
      ? `As of ${hub.data_as_of}`
      : `Updated ${formatTimestamp(headlineFreshness)}`;
  } else {
    updatedEl.textContent = "";
  }

  // Show the source so the freshness gap between hub and file is honest.
  const sourceLine = document.getElementById("source-line");
  if (hub.turnout) {
    const fileTotal = cur.total || 0;
    sourceLine.textContent = fileTotal && fileTotal !== hub.turnout
      ? `Source: GA SoS Election Data Hub · file lags at ${fmt.format(fileTotal)}`
      : `Source: GA SoS Election Data Hub`;
  } else if (cur.total) {
    sourceLine.textContent = `Source: GA SoS voter participation history file`;
  }

  const snapshots = data.snapshots || [];
  if (snapshots.length >= 2) {
    const last = snapshots[snapshots.length - 1];
    if (last.delta > 0) {
      const badge = document.getElementById("delta-badge");
      badge.textContent = `+${fmt.format(last.delta)} since last update`;
      badge.classList.remove("hidden");
    }
  }
}

function formatDay(isoDate) {
  // Render "YYYY-MM-DD" as e.g. "Sat May 2". Force noon UTC to avoid
  // off-by-one when the runtime timezone is east of UTC.
  if (!isoDate) return "";
  const d = new Date(`${isoDate}T12:00:00Z`);
  return d.toLocaleDateString(undefined, {
    weekday: "short", month: "short", day: "numeric",
  });
}

function setDailyCaption(text) {
  const el = document.getElementById("daily-caption");
  if (el) el.textContent = text;
}

function renderDaily(data) {
  const ctx = document.getElementById("daily-chart");
  if (!ctx) return;

  const t = theme();
  const hubDays = (data.hub && data.hub.by_day_party) || [];
  const fallbackDays = data.by_day || [];

  // Hub's by_day_party (Early Voting → by Party and Date) is the preferred
  // source: it's authoritative per-day-per-party turnout from the official
  // dashboard. Fall back to snapshot-delta-derived voters_added when hub
  // hasn't scraped that sheet yet.
  if (hubDays.length >= 1) {
    setDailyCaption(
      "In-person early voting per day, broken down by primary ballot pulled. " +
      "Live from the GA SoS Election Data Hub.",
    );
    const totals = hubDays.map((d) =>
      (d.democrat || 0) + (d.republican || 0) + (d.non_partisan || 0));
    const peakIdx = totals.indexOf(Math.max(...totals));
    describeCanvas(
      "daily-chart",
      `Bar chart of in-person early voting per day across ${hubDays.length} days, ` +
      `stacked by primary ballot. Highest day: ${formatDay(hubDays[peakIdx].date)} ` +
      `with ${fmt.format(totals[peakIdx])} voters.`,
      hubDays.map((d) =>
        `${formatDay(d.date)}: Democrat ${fmt.format(d.democrat || 0)}, ` +
        `Republican ${fmt.format(d.republican || 0)}, ` +
        `Non-partisan ${fmt.format(d.non_partisan || 0)}.`,
      ).join(" "),
    );
    registerChart("daily-chart", new Chart(ctx, {
      type: "bar",
      data: {
        labels: hubDays.map((d) => formatDay(d.date)),
        datasets: [
          {
            label: "Democrat",
            data: hubDays.map((d) => d.democrat || 0),
            backgroundColor: t.party.DEMOCRAT,
            stack: "party",
          },
          {
            label: "Republican",
            data: hubDays.map((d) => d.republican || 0),
            backgroundColor: t.party.REPUBLICAN,
            stack: "party",
          },
          {
            label: "Non-Partisan",
            data: hubDays.map((d) => d.non_partisan || 0),
            backgroundColor: t.party["NON-PARTISAN"],
            stack: "party",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: {
            position: "bottom",
            labels: { boxWidth: 12, padding: 12, color: t.inkSoft },
          },
          tooltip: {
            callbacks: {
              label: (ctx) =>
                `${ctx.dataset.label}: ${fmt.format(ctx.parsed.y)}`,
              footer: (items) => {
                const total = items.reduce((s, it) => s + it.parsed.y, 0);
                return `Total: ${fmt.format(total)}`;
              },
            },
          },
        },
        scales: {
          x: {
            stacked: true,
            grid: { display: false },
            ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10, color: t.tick },
          },
          y: {
            stacked: true,
            beginAtZero: true,
            ticks: { callback: (v) => fmt.format(v), color: t.tick },
            grid: { color: t.grid },
          },
        },
      },
    }));
    return;
  }

  // Empty-state placeholder: we have no hub data and no snapshot deltas yet.
  if (fallbackDays.length < 1) {
    setDailyCaption("Daily counts will appear after the next scrape.");
    const cur = data.current || {};
    ctx.parentElement.replaceChildren(buildPlaceholder({
      total: cur.total || 0,
      label: cur.scraped_at ? `as of ${formatTimestamp(cur.scraped_at)}` : "",
      note: "Daily counts will appear after the next scrape.",
    }));
    return;
  }

  setDailyCaption(
    "Voters added between snapshots, bucketed by Eastern-time calendar day. " +
    "The first snapshot is omitted because it represents a backlog from " +
    "earlier in the early-vote window.",
  );

  describeCanvas(
    "daily-chart",
    `Bar chart of voters added between snapshots across ${fallbackDays.length} days.`,
    fallbackDays.map((d) =>
      `${formatDay(d.date)}: ${fmt.format(d.voters_added)} added.`,
    ).join(" "),
  );
  registerChart("daily-chart", new Chart(ctx, {
    type: "bar",
    data: {
      labels: fallbackDays.map((d) => formatDay(d.date)),
      datasets: [{
        label: "Voters added",
        data: fallbackDays.map((d) => d.voters_added),
        backgroundColor: t.accent,
        borderRadius: 4,
        categoryPercentage: 0.85,
        barPercentage: 0.75,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => `${fmt.format(ctx.parsed.y)} voters added`,
          },
        },
      },
      scales: {
        y: {
          beginAtZero: true,
          ticks: { callback: (v) => fmt.format(v), color: t.tick },
          grid: { color: t.grid },
        },
        x: {
          grid: { display: false },
          ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10, color: t.tick },
        },
      },
    },
  }));
}

function buildPlaceholder({ total, label, note }) {
  // Safe DOM construction — never assemble HTML strings with data values.
  const root = document.createElement("div");
  root.className = "placeholder";

  const numEl = document.createElement("p");
  numEl.className = "placeholder-num";
  numEl.textContent = fmt.format(total);
  root.appendChild(numEl);

  if (label) {
    const labelEl = document.createElement("p");
    labelEl.className = "placeholder-label";
    labelEl.textContent = label;
    root.appendChild(labelEl);
  }

  if (note) {
    const noteEl = document.createElement("p");
    noteEl.className = "placeholder-note";
    noteEl.textContent = note;
    root.appendChild(noteEl);
  }

  return root;
}

function renderBreakdown(canvasId, dict, colorFn) {
  const entries = Object.entries(dict || {}).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return;
  const ctx = document.getElementById(canvasId);
  if (!ctx) return;
  const t = theme();
  const total = entries.reduce((s, [, v]) => s + v, 0);
  describeCanvas(
    canvasId,
    `Donut chart with ${entries.length} segments. ` +
    `Largest: ${entries[0][0]} at ${fmt.format(entries[0][1])}.`,
    entries.map(([k, v]) => {
      const pct = total ? Math.round((v / total) * 100) : 0;
      return `${k}: ${fmt.format(v)} (${pct}%).`;
    }).join(" "),
  );
  registerChart(canvasId, new Chart(ctx, {
    type: "doughnut",
    data: {
      labels: entries.map(([k]) => k),
      datasets: [{
        data: entries.map(([, v]) => v),
        backgroundColor: entries.map(([k], i) => colorFn ? colorFn(k, i, t) : t.palette[i % t.palette.length]),
        // Slice separator must equal the card surface in both themes, so
        // slices read as "cut into the card" rather than "outlined in white."
        borderColor: t.ring,
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      cutout: "62%",
      plugins: {
        legend: {
          position: "bottom",
          labels: { boxWidth: 12, boxHeight: 12, padding: 12, color: t.inkSoft },
        },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.label}: ${fmt.format(ctx.parsed)}`,
          },
        },
      },
    },
  }));
}

// Two-slice pie of voted vs. not-yet-voted active voters. Pie (no cutout) is
// the deliberate choice over a doughnut: the editorial point is that the red
// slice is *small*, and a centered "X%" readout would replace seeing-the-slice
// with reading-the-number. The pale --chart-empty fill for the remainder
// reads as background capacity rather than competing data, which lets even an
// 8° wedge land visually.
function renderTurnout(data) {
  const hub = data.hub || {};
  const voted = hub.turnout;
  const base = hub.active_voters;
  if (typeof voted !== "number" || typeof base !== "number" || base <= 0) return;

  const ctx = document.getElementById("turnout-chart");
  if (!ctx) return;
  const t = theme();
  const remaining = Math.max(0, base - voted);
  const pct = typeof hub.turnout_pct === "number"
    ? hub.turnout_pct
    : Math.round((voted / base) * 1000) / 10;

  describeCanvas(
    "turnout-chart",
    `Pie chart of turnout: ${pct}% of ${fmt.format(base)} active voters ` +
    `have cast a ballot.`,
    `Voted: ${fmt.format(voted)} (${pct}%). ` +
    `Not yet voted: ${fmt.format(remaining)} (${(100 - pct).toFixed(1)}%).`,
  );

  const captionEl = document.getElementById("turnout-caption");
  if (captionEl) {
    captionEl.textContent =
      `${fmt.format(voted)} of ${fmt.format(base)} active voters · ${pct}% turnout.`;
  }

  registerChart("turnout-chart", new Chart(ctx, {
    type: "pie",
    data: {
      labels: ["Voted", "Not yet voted"],
      datasets: [{
        data: [voted, remaining],
        backgroundColor: [t.accent, t.empty],
        borderColor: t.ring,
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: {
          position: "bottom",
          labels: { boxWidth: 12, boxHeight: 12, padding: 12, color: t.inkSoft },
        },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const slicePct = (ctx.parsed / base * 100).toFixed(1);
              return `${ctx.label}: ${fmt.format(ctx.parsed)} (${slicePct}%)`;
            },
          },
        },
      },
    },
  }));
}

// Build all charts from a single cached data payload. Called once on initial
// load and again on every OS theme change so the palette stays consistent
// with the active CSS tokens.
function renderAllCharts(data) {
  const hasFile = data.current && data.current.total;
  const hasHub = data.hub && data.hub.turnout;
  if (!hasFile && !hasHub) return;
  renderDaily(data);
  if (hasHub && data.hub.by_race) {
    renderBreakdown("race-chart", data.hub.by_race);
  }
  if (hasFile) {
    renderBreakdown("style-chart", data.current.by_ballot_style);
    renderBreakdown("party-chart", data.current.by_party,
      (k, i, t) => t.party[k] || t.palette[i % t.palette.length]);
  }
  renderTurnout(data);
}

async function main() {
  let data;
  // 8s ceiling — R2 typically replies in <300ms; anything past 8s on a mobile
  // link is a dead-end and should fail loudly rather than hang on "loading…"
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 8000);
  try {
    const resp = await fetch(DATA_URL, { cache: "no-cache", signal: controller.signal });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    data = await resp.json();
  } catch (e) {
    const aborted = e && e.name === "AbortError";
    document.getElementById("empty-state").classList.remove("hidden");
    document.getElementById("kicker").textContent = "Bibb County";
    document.getElementById("total").textContent = "—";
    document.getElementById("subtitle").textContent =
      aborted ? "couldn't reach the data feed" : "data not available";
    console.error("turnout fetch failed:", e);
    return;
  } finally {
    clearTimeout(timeoutId);
  }
  // Empty only if BOTH sources lack data; hub may be present before the
  // first file scrape, or vice versa.
  const hasFile = data.current && data.current.total;
  const hasHub = data.hub && data.hub.turnout;
  if (!hasFile && !hasHub) {
    document.getElementById("empty-state").classList.remove("hidden");
    return;
  }

  // Hub's race breakdown only renders if data is available; show the card
  // before the chart binds so the layout is settled at first paint.
  if (hasHub && data.hub.by_race) {
    document.getElementById("race-card").hidden = false;
  }
  if (hasHub && typeof data.hub.active_voters === "number") {
    document.getElementById("turnout-card").hidden = false;
  }

  renderHeadline(data);
  renderAllCharts(data);

  // Live theme-switch repaint. Charts bake colors into canvas at construction,
  // so on prefers-color-scheme change we destroy and rebuild from the cached
  // data payload (no refetch). Headline DOM uses CSS vars and re-paints itself.
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const onThemeChange = () => renderAllCharts(data);
  if (mq.addEventListener) {
    mq.addEventListener("change", onThemeChange);
  } else if (mq.addListener) {
    mq.addListener(onThemeChange);
  }
}

main();
