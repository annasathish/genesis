/* Genesis resilience dashboard. Vanilla JS + Chart.js. Every number comes from /api/* (computed in Python from
   runs/*.json and live Locust / controller / Toxiproxy / Docker data). Nothing is simulated in the browser. */
const $ = (id) => document.getElementById(id);
const state = { data: null, live: null, assumptions: null, charts: {}, history: [], groupIdx: 0, runId: null,
                selNode: null, prevRanks: {}, prevLiveFail: {}, tab: "live", briefFor: null };

const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x, d = 1) => (x == null ? "n/a" : (x * 100).toFixed(d) + "%");
const num = (x, d = 1) => (x == null ? "n/a" : Number(x).toLocaleString(undefined, { maximumFractionDigits: d }));
const money = (x) => {
  const cur = (state.assumptions && state.assumptions.currency) || "USD";
  try { return new Intl.NumberFormat(undefined, { style: "currency", currency: cur, maximumFractionDigits: 0 }).format(x); }
  catch { return cur + " " + num(x, 0); }
};
const KPI_LABEL = {
  error_minutes: "Error-minutes", peak_error_rate: "Peak error rate", fault_error_rate: "Fault-phase error rate",
  failed_requests: "Failed requests", checkout_loss_min: "Checkout-loss minutes", time_to_detect_s: "Time to detect",
  time_to_recover_s: "Time to recover", blast_radius_endpoints: "Blast radius (endpoints)", resilience_score: "Resilience score",
};
const fmtKpi = (k, v) => {
  if (v == null) return "n/a";
  if (k.endsWith("_rate")) return pct(v);
  if (k.endsWith("_s")) return num(v) + " s";
  if (k.endsWith("_min") || k === "error_minutes") return num(v, 3) + " min";
  if (k === "resilience_score") return num(v, 2) + "/10";
  return num(v, 0);
};
const runShort = (id) => "Run " + (state.data.metrics.findIndex((x) => x.run_id === id) + 1);
const tile = ([k, v, s, cls]) => `<div class="tile ${cls || ""}"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="d">${esc(s || "")}</div></div>`;

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(path + " -> HTTP " + r.status);
  return r.json();
}

/* ------------------------------------------------------------------ chart helpers */
function baseOptions(yFmt, title) {
  const ink2 = css("--ink-2"), grid = css("--grid"), muted = css("--muted");
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { labels: { color: ink2, boxWidth: 10, boxHeight: 10, usePointStyle: true } },
      tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${yFmt(c.parsed.y)}` } },
      title: title ? { display: true, text: title, color: ink2, align: "start", font: { weight: 600 } } : undefined,
    },
    scales: {
      x: { ticks: { color: muted, maxTicksLimit: 6 }, grid: { display: false }, border: { color: css("--axis") } },
      y: { beginAtZero: true, ticks: { color: muted, callback: yFmt, maxTicksLimit: 5 }, grid: { color: grid }, border: { display: false } },
    },
  };
}
function draw(key, cfg) {
  if (state.charts[key]) state.charts[key].destroy();
  state.charts[key] = new Chart($(key), cfg);
}
const bar = (label, data, color, extra = {}) => ({ label, data, backgroundColor: color, borderRadius: 4, borderSkipped: "start", maxBarThickness: 44, borderColor: css("--surface"), borderWidth: 1, ...extra });

/* ================================================================== LIVE */
function renderLive() {
  const L = state.live; if (!L) return;
  $("status").dataset.level = L.status.level;
  $("status-text").textContent = L.status.text;
  $("sources").innerHTML = Object.entries(L.sources).map(([k, ok]) => `<span class="${ok ? "on" : ""}" title="${k}: ${ok ? "connected" : "unreachable"}">${k}</span>`).join("");

  const w = L.window, ks = L.kill_switch;
  const faults = L.active_faults.length + L.active_toxics.length;
  $("live-tiles").innerHTML = [
    ["Error rate (live window)", w ? pct(w.err, 2) : "—", w ? `${num(w.fail, 0)} of ${num(w.req, 0)} requests failed` : "waiting for Locust", w && w.err > ks.max_error_rate ? "alert" : ""],
    ["p95 latency", w && w.p95_ms != null ? num(w.p95_ms, 0) + " ms" : "—", ks.p95_latency_threshold_ms ? `kill-switch at ${num(ks.p95_latency_threshold_ms, 0)} ms` : "rolling Locust p95"],
    ["Throughput", w && w.rps != null ? num(w.rps, 1) + " req/s" : "—", L.users != null ? `${L.users} simulated shoppers` : ""],
    ["Kill switch", ks.tripped ? "TRIPPED" : ks.breach_windows ? `BREACH ${ks.breach_windows}/${ks.consecutive_breach_windows}` : "ARMED",
      `error > ${pct(ks.max_error_rate, 0)}${ks.p95_latency_threshold_ms ? " or p95 > " + num(ks.p95_latency_threshold_ms, 0) + " ms" : ""} for ${ks.consecutive_breach_windows} windows`,
      ks.tripped ? "alert" : ks.breach_windows ? "warn" : ""],
    ["Active faults", faults, faults ? [...L.active_faults.map((a) => `${a.fault} → ${a.target}`), ...L.active_toxics.map((t) => `${t.toxic} on ${t.proxy}`)].join(", ") : "none injected", faults ? "alert" : ""],
  ].map(tile).join("");

  renderTopology(); renderVboard(); renderLiveCharts();
}

function renderTopology() {
  const L = state.live, W = 760, H = 300, nw = 92, nh = 40;
  const rows = [["gateway", 40], ["service", 150], ["data", 260]];
  const order = { service: ["catalogue", "ratings", "cart", "shipping", "user", "payment", "dispatch"], data: ["mongodb", "mysql", "redis", "rabbitmq", "payment-gateway"] };
  const pos = {};
  rows.forEach(([tier, y]) => {
    const names = order[tier] || L.tiers[tier];
    names.forEach((n, i) => { pos[n] = { x: (W / (names.length + 1)) * (i + 1), y, tier }; });
  });
  const svc = L.services;
  const targets = new Set(L.active_faults.map((a) => a.target));
  const bad = (n) => svc[n] && svc[n].failing;
  const edges = L.edges.filter(([a, b]) => pos[a] && pos[b]).map(([a, b]) => {
    const p = pos[a], q = pos[b];
    const hot = bad(b) ? "hot" : "";
    if (p.tier === q.tier) {  // same-row call: arc above the row
      const mx = (p.x + q.x) / 2, dy = Math.min(48, 14 + Math.abs(p.x - q.x) / 6);
      return `<path class="edge ${hot}" d="M${p.x},${p.y - nh / 2} Q${mx},${p.y - nh / 2 - dy} ${q.x},${q.y - nh / 2}"/>`;
    }
    return `<path class="edge ${hot}" d="M${p.x},${p.y + nh / 2} C${p.x},${(p.y + q.y) / 2} ${q.x},${(p.y + q.y) / 2} ${q.x},${q.y - nh / 2}"/>`;
  }).join("");
  const nodes = Object.entries(pos).map(([n, p]) => {
    const s = svc[n] || {};
    const cls = [targets.has(n) ? "target" : "", s.failing ? "bad" : s.container ? "ok" : "", state.selNode === n ? "sel" : ""].join(" ");
    const sub = s.container ? (s.container.up ? (s.live_error_rate ? pct(s.live_error_rate, 0) + " err" : "up") : s.container.state) : "";
    return `<g class="node ${cls}" data-n="${esc(n)}" transform="translate(${p.x - nw / 2},${p.y - nh / 2})"><title>${esc(n)}</title>
      <rect width="${nw}" height="${nh}"></rect><text x="${nw / 2}" y="${sub ? 15 : nh / 2}">${esc(n === "payment-gateway" ? "pay-gateway" : n)}</text>
      ${sub ? `<text class="sub" x="${nw / 2}" y="29">${esc(sub)}</text>` : ""}</g>`;
  }).join("");
  $("topo").innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Service dependency map">${edges}${nodes}</svg>`;
  $("topo").querySelectorAll(".node").forEach((g) => g.addEventListener("click", () => { state.selNode = g.dataset.n; renderTopology(); renderNodeDetail(); }));
  renderNodeDetail();
}

function renderNodeDetail() {
  const n = state.selNode; if (!n || !state.live) return;
  const s = state.live.services[n] || {};
  const v = (state.live.vulnerability.rows.find((r) => r.service === n)) || null;
  const callers = state.live.edges.filter(([, d]) => d === n).map(([c]) => c);
  const deps = state.live.edges.filter(([c]) => c === n).map(([, d]) => d);
  const c = s.container;
  $("node-detail").innerHTML = `<b>${esc(n)}</b> · ${c ? `container <b>${esc(c.state)}</b>${c.health !== "n/a" ? " (" + esc(c.health) + ")" : ""}` : "no container info"}
    ${s.live_requests != null ? ` · live error <b>${pct(s.live_error_rate)}</b> over ${s.live_requests} req` : ""}<br>
    Calls: ${esc(deps.join(", ") || "—")} · Called by: ${esc(callers.join(", ") || "—")}<br>
    ${v ? (v.score != null ? `H<sub>v</sub> <b>${v.score}</b> (${esc(v.status)}, ${v.runs} run${v.runs > 1 ? "s" : ""}) · cascade (${esc(v.cascade_source)}): ${esc(v.cascade.join(", ") || "none")}`
          : `Not tested yet · predicted cascade: ${esc(v.cascade.join(", ") || "none")}`) : ""}`;
}

function renderVboard() {
  const V = state.live.vulnerability;
  $("coverage").textContent = `${V.coverage.tested}/${V.coverage.total} services tested`;
  $("hv-formula").textContent = V.formula + ". Untested services get no score: an untested service is a coverage gap, not \"stable\". Cascade is measured from failing endpoints when the service was tested, otherwise predicted from the dependency map.";
  $("vboard").innerHTML = V.rows.map((r) => {
    const moved = state.prevRanks[r.service] != null && state.prevRanks[r.service] !== r.rank;
    const newlyFailing = r.live_failing && !state.prevLiveFail[r.service];
    return `<li class="${r.live_failing ? "live" : ""} ${moved || newlyFailing ? "flash" : ""}">
      <span class="rk">#${r.rank}</span>
      <div><div class="nm">${esc(r.service)} ${r.live_failing ? '<span class="st CRITICAL">LIVE FAILING</span>' : ""}</div>
        <div class="cas">${r.cascade.length ? "breaks: " + esc(r.cascade.join(", ")) : "no downstream callers"}${r.cascade_source.startsWith("predicted") ? " (predicted)" : ""}</div>
        ${r.score != null ? `<div class="bar"><i style="width:${Math.min(100, r.score)}%"></i></div>` : ""}</div>
      <div class="sc">${r.score != null ? `<b>${r.score}</b><span class="muted">/100</span>` : '<span class="muted">—</span>'}<br><span class="st ${r.status}">${r.status}</span></div></li>`;
  }).join("");
  V.rows.forEach((r) => { state.prevRanks[r.service] = r.rank; state.prevLiveFail[r.service] = r.live_failing; });
}

function renderLiveCharts() {
  const h = state.live.history;
  const labels = h.map((p) => new Date(p.ts * 1000).toLocaleTimeString([], { minute: "2-digit", second: "2-digit" }));
  const series = [
    ["c-rps", "Throughput (req/s)", h.map((p) => p.rps), (v) => num(v, 1)],
    ["c-err", "Error rate (%)", h.map((p) => p.err * 100), (v) => num(v, 1) + "%"],
    ["c-p95", "p95 latency (ms)", h.map((p) => p.p95_ms), (v) => num(v, 0) + " ms"],
  ];
  series.forEach(([id, label, data, fmt]) => {
    const ch = state.charts[id];
    if (ch && ch.$theme === document.documentElement.dataset.theme) {
      ch.data.labels = labels; ch.data.datasets[0].data = data; ch.update("none"); return;
    }
    const o = baseOptions(fmt, label); o.plugins.legend = { display: false };
    draw(id, { type: "line", data: { labels, datasets: [{ label, data, borderColor: css("--s1"), backgroundColor: css("--s1"), borderWidth: 2, pointRadius: 0, pointHoverRadius: 5, tension: 0.25 }] }, options: o });
    state.charts[id].$theme = document.documentElement.dataset.theme;
  });
}

/* ================================================================== EXPERIMENTS */
const signedChange = (rel) => (rel == null ? "n/a" : (rel > 0 ? "−" : "+") + pct(Math.abs(rel)));
const verdictText = (v) => ({ improved: "significant improvement", regressed: "significant regression", mixed: "mixed signals", "no significant change": "not significant (noise)" }[v] || v);
const badge = (v) => `<span class="badge ${{ improved: "b-improved", regressed: "b-regressed", mixed: "b-mixed" }[v] || "b-none"}"><span class="dot"></span>${esc(verdictText(v))}</span>`;

function renderTiles() {
  const d = state.data, m = d.metrics;
  if (!m.length) { $("tiles").innerHTML = tile(["No runs yet", "–", "run python scripts/day1_demo.py"]); return; }
  const latest = m[m.length - 1];
  const g = d.groups.find((x) => x.runs.includes(latest.run_id));
  const cum = g && g.cumulative;
  $("tiles").innerHTML = [
    ["Runs recorded", m.length, `${d.groups.length} experiment type(s)`],
    ["Latest resilience score", num(latest.resilience_score, 2) + "/10", `${latest.fault} on ${latest.target}`],
    ["Latest error-minutes", num(latest.error_minutes, 3), `peak ${pct(latest.peak_error_rate)} · recovered in ${num(latest.time_to_recover_s)} s`],
    ["First → latest error-minutes", cum ? signedChange(cum.primary_relative_change) : "–", cum ? verdictText(cum.verdict) : "needs 2+ like-for-like runs"],
  ].map(tile).join("");
}

function renderGroups() {
  const sel = $("group"), prev = sel.value;
  sel.innerHTML = state.data.groups.map((g, i) => `<option value="${i}">${esc(g.fault)} → ${esc(g.target)} (${g.runs.length} runs)</option>`).join("");
  if (prev && +prev < state.data.groups.length) sel.value = prev;
  state.groupIdx = +sel.value || 0;
  renderTrend();
}

function renderTrend() {
  const g = state.data.groups[state.groupIdx];
  if (!g) { $("steps").innerHTML = `<tr><td class="empty">No runs yet.</td></tr>`; return; }
  const kpi = $("trend-kpi").value, fmt = (v) => fmtKpi(kpi, v);
  const labels = g.trend.map((t) => runShort(t.run_id) + (t.label ? " · " + t.label : ""));
  const o1 = baseOptions(fmt, KPI_LABEL[kpi] + " per run"); o1.plugins.legend = { display: false };
  draw("trend", { type: "bar", data: { labels, datasets: [bar(KPI_LABEL[kpi], g.trend.map((t) => t[kpi]), css("--s1"))] }, options: o1 });
  const sfmt = (v) => num(v) + " s";
  draw("times", { type: "bar", data: { labels, datasets: [
    bar("Time to detect", g.trend.map((t) => t.time_to_detect_s), css("--s1")),
    bar("Time to recover", g.trend.map((t) => t.time_to_recover_s), css("--s2"))] }, options: baseOptions(sfmt, "Detection & recovery (s)") });

  const rows = g.steps.length ? g.steps.concat(g.cumulative ? [{ ...g.cumulative, _cum: true }] : []) : [];
  const cell = (dl) => `<td class="num ${dl.better === true ? "better" : dl.better === false ? "worse" : ""}" title="${esc(dl.note || "")}">${esc(fmtKpi(dl.kpi, dl.old))} → ${esc(fmtKpi(dl.kpi, dl.new))}</td>`;
  $("steps").innerHTML = `<tr><th>Step</th><th>Verdict</th><th class="num">Error-minutes</th><th class="num">Change</th><th class="num">Fault error rate</th><th class="num">p-value</th><th class="num">Detect</th><th class="num">Recover</th><th class="num">Score</th></tr>` +
    (rows.length ? rows.map((s) => `<tr><td>${s._cum ? "<b>First → latest</b>" : esc(runShort(s.from) + " → " + runShort(s.to))}${s.to_label ? `<br><span class="hint">${esc(s.to_label)}</span>` : ""}</td>
      <td>${badge(s.verdict)}</td>${cell(s.deltas.error_minutes)}
      <td class="num ${s.primary_relative_change > 0 ? "better" : s.primary_relative_change < 0 ? "worse" : ""}">${signedChange(s.primary_relative_change)}</td>
      ${cell(s.deltas.fault_error_rate)}<td class="num">${s.significance.p_value ?? "n/a"}</td>
      ${cell(s.deltas.time_to_detect_s)}${cell(s.deltas.time_to_recover_s)}${cell(s.deltas.resilience_score)}</tr>`).join("")
      : `<tr><td colspan="9" class="empty">Only one run of this experiment. Re-run it after a fix (with <code>--label</code>) to measure improvement.</td></tr>`);
  $("method").innerHTML = Object.entries(state.data.method).map(([k, v]) => `<li><b>${esc(k.replace(/_/g, " "))}:</b> ${esc(v)}</li>`).join("");
}

function renderRunSelect() {
  const m = state.data.metrics;
  const keep = state.runId && m.some((x) => x.run_id === state.runId) ? state.runId : (m.length ? m[m.length - 1].run_id : null);
  const opts = m.map((x, i) => `<option value="${esc(x.run_id)}">Run ${i + 1} · ${esc(x.fault)} → ${esc(x.target)}${x.label ? " · " + esc(x.label) : ""}</option>`).join("");
  $("run-select").innerHTML = opts;
  const bsel = $("brief-run"), bkeep = bsel.value;
  bsel.innerHTML = opts;
  bsel.value = bkeep && m.some((x) => x.run_id === bkeep) ? bkeep : (keep || "");
  if (keep) $("run-select").value = keep;
  state.runId = keep;
  renderTimeline();
}

function renderTimeline() {
  const run = state.data.runs.find((r) => r.run_id === state.runId);
  const m = state.data.metrics.find((r) => r.run_id === state.runId);
  if (!run) return;
  $("run-facts").textContent = `peak ${pct(m.peak_error_rate)} · detect ${num(m.time_to_detect_s)} s · recover ${num(m.time_to_recover_s)} s · down: ${(m.services_down || []).join(", ") || "none"}`;
  const pts = run.windows.map((w) => ({ x: w.t, y: w.err * 100, w }));
  const tmax = Math.max(...run.windows.map((w) => w.t), 10);
  const firstRec = run.windows.find((w) => w.phase === "recovery");
  const bands = { id: "bands", beforeDatasetsDraw(chart) {
    const { ctx, chartArea: a, scales: { x } } = chart;
    const split = firstRec ? x.getPixelForValue(firstRec.t - run.window_s) : a.right;
    ctx.save();
    ctx.fillStyle = css("--phase-fault"); ctx.fillRect(x.getPixelForValue(0), a.top, split - x.getPixelForValue(0), a.bottom - a.top);
    if (firstRec) { ctx.fillStyle = css("--phase-rec"); ctx.fillRect(split, a.top, a.right - split, a.bottom - a.top); }
    ctx.restore();
  } };
  const yfmt = (v) => num(v, 0) + "%";
  const o = baseOptions(yfmt);
  o.scales.x = { type: "linear", min: 0, max: tmax, title: { display: true, text: "seconds since injection", color: css("--muted") }, ticks: { color: css("--muted") }, grid: { display: false }, border: { color: css("--axis") } };
  o.scales.y.max = 100;
  o.plugins.tooltip = { callbacks: {
    title: (items) => `+${items[0].parsed.x}s (${items[0].raw.w ? items[0].raw.w.phase : ""})`,
    label: (c) => c.dataset.label === "Error rate" ? `Error rate: ${num(c.parsed.y, 1)}% of ${c.raw.w.req} req` : `${c.dataset.label}: ${num(c.parsed.y, 1)}%`,
    afterBody: (items) => { const w = items[0].raw.w; return w && w.failing.length ? ["failing: " + w.failing.join(", ")] : []; },
  } };
  draw("timeline", { type: "line", plugins: [bands], data: { datasets: [
    { label: "Error rate", data: pts, borderColor: css("--s1"), backgroundColor: css("--s1"), borderWidth: 2, pointRadius: 3, pointHoverRadius: 6, tension: 0.2 },
    { label: "Kill-switch threshold", data: [{ x: 0, y: run.baseline.threshold * 100 }, { x: tmax, y: run.baseline.threshold * 100 }], borderColor: css("--muted"), borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0 },
  ] }, options: o });
}

/* ================================================================== BUSINESS */
const ASSUME_FIELDS = [
  ["orders_per_minute", "Orders / minute (prod)", 1], ["avg_order_value", "Avg order value", 1],
  ["customer_retry_rate", "Customers who retry (0–1)", 0.05], ["incident_cost_per_minute", "Ops cost / incident min", 1],
  ["support_ticket_rate", "Tickets per lost customer", 0.05], ["cost_per_ticket", "Cost per ticket", 1],
  ["incidents_per_year", "Incidents / year", 1], ["slo_target", "SLO target", 0.0005], ["currency", "Currency", null],
];
function renderAssumptions() {
  const a = state.assumptions;
  $("assume").innerHTML = ASSUME_FIELDS.map(([k, label, step]) =>
    `<label>${esc(label)}<input data-k="${k}" ${step ? `type="number" step="${step}" min="0"` : 'type="text" maxlength="5"'} value="${esc(a[k])}"></label>`).join("");
  $("assume").querySelectorAll("input").forEach((inp) => inp.addEventListener("change", async () => {
    state.assumptions[inp.dataset.k] = inp.type === "number" ? parseFloat(inp.value) : inp.value;
    state.data.business = await api("/api/business", state.assumptions);
    state.assumptions = state.data.business.assumptions;
    renderBusiness();
  }));
}

function renderBusiness() {
  const b = state.data.business, rows = b.runs, w = b.worst_run;
  const g = b.trends.find((t) => t.cumulative);
  const grp = g && state.data.groups.find((x) => x.signature === g.signature);
  const gv = grp && grp.cumulative ? grp.cumulative.verdict : null;
  $("biz-tiles").innerHTML = (w ? [
    ["Worst cost per incident", money(w.cost_per_incident), runShort(w.run_id) + (w.estimated ? " · estimated" : "")],
    ["Orders lost (worst)", num(w.orders_lost), `revenue at risk ${money(w.revenue_at_risk)}`],
    ["Annual exposure (worst)", money(w.annual_exposure), `${num(b.assumptions.incidents_per_year, 0)} incidents/yr`],
    ["Error budget per incident", w.slo_budget_burn_pct + "%", `of ${num(w.error_budget_minutes)} min/month (${(b.assumptions.slo_target * 100).toFixed(2)}% SLO)`],
    ["Support tickets (worst)", num(w.support_tickets), `${money(w.support_cost)} handling cost`],
    ["First → latest cost change", g ? money(-g.cumulative.saved_per_year) + "/yr" : "–", g ? (gv === "improved" ? "significant: a real saving" : verdictText(gv) + ": not claimed as a saving") : "needs 2+ runs"],
  ] : []).map(tile).join("");
  const labels = rows.map((r) => runShort(r.run_id) + (r.label ? " · " + r.label : ""));
  const o = baseOptions(money, "Projected cost per incident"); o.scales.x.stacked = true; o.scales.y.stacked = true;
  draw("cost", { type: "bar", data: { labels, datasets: [
    bar("Revenue lost (net)", rows.map((r) => r.revenue_lost), css("--s1"), { stack: "c" }),
    bar("Ops / incident cost", rows.map((r) => r.ops_cost), css("--s2"), { stack: "c" }),
    bar("Support cost", rows.map((r) => r.support_cost), css("--s3"), { stack: "c" })] }, options: o });
  $("biz-table").innerHTML = `<tr><th>Run</th><th>Churn risk</th><th class="num">Checkout-loss</th><th class="num">Orders lost</th><th class="num">Revenue at risk</th><th class="num">Tickets</th><th class="num">Per incident</th><th class="num">Per year</th><th class="num">SLO budget</th></tr>` +
    rows.map((r) => { const m = state.data.metrics.find((x) => x.run_id === r.run_id);
      return `<tr><td>${esc(runShort(r.run_id))}${r.estimated ? ' <span class="hint" title="older run: checkout loss estimated from the aggregate error rate">est.</span>' : ""}</td>
      <td class="churn-${r.churn_risk}"><b>${r.churn_risk}</b></td>
      <td class="num">${num(m.checkout_loss_min, 3)} min</td><td class="num">${num(r.orders_lost)}</td><td class="num">${money(r.revenue_at_risk)}</td>
      <td class="num">${num(r.support_tickets)}</td><td class="num"><b>${money(r.cost_per_incident)}</b></td>
      <td class="num">${money(r.annual_exposure)}</td><td class="num">${r.slo_budget_burn_pct}%</td></tr>`; }).join("");
  $("biz-note").textContent = (b.any_estimated ? "Runs marked est. predate per-endpoint recording, so their checkout loss is estimated from the aggregate error rate; new runs measure lost checkouts against the baseline checkout rate. " : "") +
    "Churn risk: HIGH if the checkout flow failed with peak error ≥ 50 %, MEDIUM if any checkout endpoint failed, peak error ≥ 20 % or p95 > 2 s. Savings are only claimed where the improvement verdict is significant.";
  $("formulas").innerHTML = Object.entries(b.formulas).map(([k, v]) => `<li><b>${esc(k)}</b> = ${esc(v)}</li>`).join("");
  renderFunnel();
}

function renderFunnel() {
  const r = state.data.business.runs.find((x) => x.run_id === $("brief-run").value);
  if (!r) { $("funnel").innerHTML = ""; return; }
  $("funnel").innerHTML = `<span class="hint" style="align-self:center">Shopper journey:</span>` + r.funnel.map((f) =>
    `<span class="stg ${f.blocked.length ? "blocked" : "ok"}" title="${esc(f.blocked.join(", "))}">${f.blocked.length ? "✕ " : "✓ "}${esc(f.stage)}</span>`).join("") +
    `<span class="stg churn-${r.churn_risk}"><b>Churn risk ${r.churn_risk}</b></span>`;
}

async function loadBrief() {
  const id = $("brief-run").value; if (!id) return;
  state.briefFor = id;
  $("brief").innerHTML = '<span class="hint">Writing brief…</span>'; $("brief-meta").textContent = "";
  renderFunnel();
  try {
    const r = await api("/api/brief/" + encodeURIComponent(id), state.assumptions);
    if (state.briefFor !== id) return;
    $("brief").innerHTML = mdLite(r.brief).replace(/^(What happened:|What it affects:|Time and money:|Recommendation:)/gm, '<span class="lbl">$1</span>');
    $("brief-meta").textContent = r.mode === "llm" ? `Written by ${r.model} from the measured numbers (it is not allowed to invent figures).` : `Rule-based brief${r.note ? " · AI unavailable: " + r.note : ""}`;
  } catch (e) { $("brief").textContent = "Error: " + e.message; }
}

/* ================================================================== LOAD */
async function loadOverview() {
  try {
    const d = await api("/api/overview");
    if (state.assumptions) d.business = await api("/api/business", state.assumptions);
    const first = !state.data;
    state.data = d; state.assumptions = d.business.assumptions;
    if (first) renderAssumptions();
    renderTiles(); renderGroups(); renderRunSelect(); renderBusiness();
    if (first && state.tab === "business") loadBrief();
  } catch (e) { console.error(e); }
}
async function loadLive() {
  try { state.live = await api("/api/live"); renderLive(); }
  catch (e) { $("status").dataset.level = "unknown"; $("status-text").textContent = "dashboard API unreachable"; }
}

/* ================================================================== CHAT */
// Minimal, safe markdown for model answers: escape everything first, then allow **bold**, `code`, "* " bullets.
const mdLite = (t) => esc(t)
  .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
  .replace(/`([^`\n]+)`/g, "<code>$1</code>")
  .replace(/^\s*[*-]\s+/gm, "• ");
function addMsg(role, text, meta) {
  const div = document.createElement("div");
  div.className = "msg " + (role === "user" ? "user" : "bot");
  if (role === "user") div.textContent = text; else div.innerHTML = mdLite(text);
  if (meta) { const s = document.createElement("span"); s.className = "meta"; s.textContent = meta; div.appendChild(s); }
  $("chat-log").appendChild(div); $("chat-log").scrollTop = $("chat-log").scrollHeight;
  return div;
}
async function ask(q) {
  q = q.trim(); if (!q) return;
  $("chat-q").value = "";
  addMsg("user", q);
  const pending = addMsg("bot", "Thinking…");
  try {
    const r = await api("/api/chat", { question: q, history: state.history, assumptions: state.assumptions });
    pending.remove();
    addMsg("bot", r.answer, (r.mode === "llm" ? r.model : "offline rules") + (r.note ? " · " + r.note : ""));
    state.history.push({ role: "user", text: r.masked_question }, { role: "assistant", text: r.answer });
    state.history = state.history.slice(-8);
  } catch (e) { pending.textContent = "Error: " + e.message; }
}
const CHIPS = ["What is happening right now?", "Did resilience improve between runs?", "What is the business impact?", "Which service is most fragile?", "What should we fix first?", "How is improvement calculated?"];

/* ================================================================== WIRING */
function setTab(t) {
  state.tab = t;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
  document.querySelectorAll(".tab-panel").forEach((p) => { p.hidden = p.dataset.panel !== t; });
  try { history.replaceState(null, "", "#" + t); } catch {}
  Object.values(state.charts).forEach((c) => c.resize());
  if (t === "business" && state.data && !state.briefFor) loadBrief();
}
function init() {
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
  $("chips").innerHTML = CHIPS.map((c) => `<button type="button">${esc(c)}</button>`).join("");
  $("chips").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));
  $("chat-form").addEventListener("submit", (e) => { e.preventDefault(); ask($("chat-q").value); });
  $("group").addEventListener("change", (e) => { state.groupIdx = +e.target.value; renderTrend(); });
  $("trend-kpi").addEventListener("change", renderTrend);
  $("run-select").addEventListener("change", (e) => { state.runId = e.target.value; renderTimeline(); });
  $("brief-run").addEventListener("change", renderFunnel);
  $("brief-go").addEventListener("click", loadBrief);
  $("theme").addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("genesis-theme", root.dataset.theme); } catch {}
    if (state.data) { renderTrend(); renderTimeline(); renderBusiness(); }
    if (state.live) renderLive();
  });
  try { const t = localStorage.getItem("genesis-theme"); if (t) document.documentElement.dataset.theme = t; } catch {}
  api("/api/health").then((h) => {
    $("chat-model").textContent = h.llm ? h.model : "offline";
    $("chat-mode").textContent = h.llm ? "AI answers grounded in measured runs and live telemetry." : "No GEMINI_API_KEY yet: answering from local rules. Add it to controller/.env, no restart needed.";
  }).catch(() => {});
  addMsg("bot", "Hi! Ask me what is happening right now, how a run went, whether we improved, what it costs the business, or what to fix first.");
  const fromHash = () => { const t = (location.hash || "").slice(1); return ["live", "experiments", "business"].includes(t) ? t : "live"; };
  window.addEventListener("hashchange", () => { if (fromHash() !== state.tab) setTab(fromHash()); });
  setTab(fromHash());
  loadLive(); loadOverview();
  setInterval(() => { if (document.visibilityState === "visible") loadLive(); }, 3000);
  setInterval(() => { if (document.visibilityState === "visible") loadOverview(); }, 20000);
}
init();
