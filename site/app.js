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

const ACCENT = "#a32638";
const ACCENT_SOFT = "rgba(163, 38, 56, 0.18)";
const PALETTE = ["#a32638", "#3b82f6", "#0ea5a4", "#9333ea", "#f59e0b", "#475569"];

const PARTY_COLOR = {
  DEMOCRAT: "#3b82f6",
  REPUBLICAN: "#a32638",
  "NON-PARTISAN": "#475569",
};

const fmt = new Intl.NumberFormat("en-US");

const ELECTION_NAME_OVERRIDE = {
  // Friendly names for known elections, since the source CSV only has the type.
  "A-12599": "May 19, 2026 General Primary",
};

function electionDisplay(election) {
  if (!election || !election.id) return "";
  return ELECTION_NAME_OVERRIDE[election.id] ||
    `${election.type} · ${election.date}`;
}

function formatTimestamp(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    weekday: "short", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit", timeZoneName: "short",
  });
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
  document.getElementById("total").textContent = fmt.format(headlineTotal);
  document.getElementById("updated").textContent =
    headlineFreshness
      ? (hub.data_as_of
          ? `As of ${hub.data_as_of}`
          : `Updated ${formatTimestamp(headlineFreshness)}`)
      : "";

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

function renderDaily(data) {
  const ctx = document.getElementById("daily-chart");
  if (!ctx) return;

  const hubDays = (data.hub && data.hub.by_day_party) || [];
  const fallbackDays = data.by_day || [];

  // Hub's by_day_party (Early Voting → by Party and Date) is the preferred
  // source: it's authoritative per-day-per-party turnout from the official
  // dashboard. Fall back to snapshot-delta-derived voters_added when hub
  // hasn't scraped that sheet yet.
  if (hubDays.length >= 1) {
    new Chart(ctx, {
      type: "bar",
      data: {
        labels: hubDays.map((d) => formatDay(d.date)),
        datasets: [
          {
            label: "Democrat",
            data: hubDays.map((d) => d.democrat || 0),
            backgroundColor: PARTY_COLOR.DEMOCRAT,
            stack: "party",
          },
          {
            label: "Republican",
            data: hubDays.map((d) => d.republican || 0),
            backgroundColor: PARTY_COLOR.REPUBLICAN,
            stack: "party",
          },
          {
            label: "Non-Partisan",
            data: hubDays.map((d) => d.non_partisan || 0),
            backgroundColor: PARTY_COLOR["NON-PARTISAN"],
            stack: "party",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { position: "bottom", labels: { boxWidth: 12, padding: 12 } },
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
            ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10 },
          },
          y: {
            stacked: true,
            beginAtZero: true,
            ticks: { callback: (v) => fmt.format(v) },
            grid: { color: "rgba(0,0,0,0.05)" },
          },
        },
      },
    });
    return;
  }

  // Empty-state placeholder: we have no hub data and no snapshot deltas yet.
  if (fallbackDays.length < 1) {
    const cur = data.current || {};
    ctx.parentElement.replaceChildren(buildPlaceholder({
      total: cur.total || 0,
      label: cur.scraped_at ? `as of ${formatTimestamp(cur.scraped_at)}` : "",
      note: "Daily counts will appear after the next scrape.",
    }));
    return;
  }

  new Chart(ctx, {
    type: "bar",
    data: {
      labels: fallbackDays.map((d) => formatDay(d.date)),
      datasets: [{
        label: "Voters added",
        data: fallbackDays.map((d) => d.voters_added),
        backgroundColor: ACCENT,
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
          ticks: { callback: (v) => fmt.format(v) },
          grid: { color: "rgba(0,0,0,0.05)" },
        },
        x: {
          grid: { display: false },
          ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10 },
        },
      },
    },
  });
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
  new Chart(ctx, {
    type: "doughnut",
    data: {
      labels: entries.map(([k]) => k),
      datasets: [{
        data: entries.map(([, v]) => v),
        backgroundColor: entries.map(([k], i) => colorFn ? colorFn(k, i) : PALETTE[i % PALETTE.length]),
        borderColor: "#ffffff",
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
          labels: { boxWidth: 12, boxHeight: 12, padding: 12 },
        },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.label}: ${fmt.format(ctx.parsed)}`,
          },
        },
      },
    },
  });
}

async function main() {
  let data;
  try {
    const resp = await fetch(DATA_URL, { cache: "no-cache" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    data = await resp.json();
  } catch (e) {
    document.getElementById("empty-state").classList.remove("hidden");
    document.getElementById("kicker").textContent = "Bibb County";
    document.getElementById("total").textContent = "—";
    document.getElementById("subtitle").textContent = "data not available";
    console.error("turnout fetch failed:", e);
    return;
  }
  // Empty only if BOTH sources lack data; hub may be present before the
  // first file scrape, or vice versa.
  const hasFile = data.current && data.current.total;
  const hasHub = data.hub && data.hub.turnout;
  if (!hasFile && !hasHub) {
    document.getElementById("empty-state").classList.remove("hidden");
    return;
  }
  renderHeadline(data);
  renderDaily(data);
  if (hasHub && data.hub.by_race) {
    document.getElementById("race-card").hidden = false;
    renderBreakdown("race-chart", data.hub.by_race);
  }
  if (hasFile) {
    renderBreakdown("style-chart", data.current.by_ballot_style);
    renderBreakdown("party-chart", data.current.by_party,
      (k, i) => PARTY_COLOR[k] || PALETTE[i % PALETTE.length]);
  }
}

main();
