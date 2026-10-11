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
  failed_requests: "Failed requests", checkout_loss_min: "Checkout-loss minutes",
  checkout_peak_loss: "Worst checkout failure", slowest_endpoint_ms: "Slowest endpoint latency", time_to_detect_s: "Time to detect",
  time_to_recover_s: "Time to recover", blast_radius_endpoints: "Blast radius (endpoints)", resilience_score: "Resilience score",
};
const fmtKpi = (k, v) => {
  if (v == null) return "n/a";
  if (k.endsWith("_rate") || k === "checkout_peak_loss") return pct(v);
  if (k.endsWith("_ms")) return num(v, 0) + " ms";
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

/* ================================================================== PLAIN LANGUAGE */
function plainName(fault, target, params) {
  const [caller, dep] = String(target).split("_to_");
  if (fault === "container_crash") return `${cap(target)} service crashed`;
  if (fault === "network_latency") return `Slow network: ${caller} → ${dep}${params && params.latency_ms ? ` (+${params.latency_ms / 1000} s)` : ""}`;
  if (fault === "rabbitmq_block") return "Order queue (RabbitMQ) stopped";
  if (fault === "disk_fill") return `${cap(target)} disk filled up`;
  return `${fault} on ${target}`;
}
const cap = (x) => String(x).charAt(0).toUpperCase() + String(x).slice(1);

function renderBanner() {
  const L = state.live, b = $("live-banner");
  const svc = L.services, order = ["web", "catalogue", "user", "cart", "shipping", "ratings", "payment"];
  const down = order.filter((n) => svc[n] && svc[n].failing);
  const faults = [...L.active_faults.map((f) => plainName(f.fault, f.target)), ...L.active_toxics.map((t) => plainName("network_latency", t.proxy))];
  const ap = state.ap || {};
  let lvl, icon, title, sub;
  const since = Math.min(...Object.values(svc).map((x) => x.last_outage && x.last_outage.end == null ? x.last_outage.start : Infinity));
  if (faults.length) { lvl = "bad"; icon = "🔴"; title = `Fault injected: ${faults.join(", ")}`; sub = down.length ? `Down right now: ${down.join(", ")}. The kill switch will roll it back automatically.` : "Watching the impact; rollback is automatic."; }
  else if (down.length) { lvl = "bad"; icon = "🔴"; title = `${down.map(cap).join(", ")} ${down.length > 1 ? "are" : "is"} down`; sub = "See the table below for the proof."; }
  else if (L.status.level === "recovering") { lvl = "warn"; icon = "🟠"; title = "Recovering after an experiment"; sub = "Errors are settling back to normal."; }
  else if (L.status.level === "unknown") { lvl = "warn"; icon = "⚪"; title = "Waiting for live data"; sub = L.status.text; }
  else { lvl = "good"; icon = "✅"; title = `All ${order.length} shop services are healthy`; sub = "Kill switch armed · safe to start a test."; }
  if (ap.state === "running") sub = `Autopilot running: step ${(ap.steps || []).filter((x) => ["done", "failed", "skipped"].includes(x.status)).length + 1} of ${(ap.steps || []).length}. ` + sub;
  b.dataset.level = lvl; $("live-banner-icon").textContent = icon; $("live-banner-title").textContent = title;
  $("live-banner-sub").innerHTML = esc(sub) + (isFinite(since) ? ` <b class="worse">· outage running for <span data-since="${since}">${Math.round(Date.now() / 1000 - since)} s</span></b>` : "");
}

/* ================================================================== LIVE EVENT FEED */
const PHASES = ["Baseline", "Fault injected", "Problem detected", "Rolled back", "Recovered"];
// Turns scripts/day1_demo.py output into plain-language events + the current phase + the live error rate.
function parseRun(lines) {
  const ev = [], seen = new Set();
  let phase = -1, err = null, fault = null, target = null, detect = null, rec = null, intent = null;
  const once = (key, e) => { if (!seen.has(key)) { seen.add(key); ev.push(e); } };
  for (const raw of lines || []) {
    const l = raw.trim();
    let m;
    if (l.startsWith("[1] baseline")) { phase = 0; once("b", { i: "📏", t: "Measuring normal shop traffic for 30 s (baseline)" }); }
    else if ((m = l.match(/^baseline error rate ([\d.]+%), p95 (\S+) ms/))) once("bn", { i: "✔️", t: `Normal behaviour: ${m[1]} errors, p95 ${m[2]} ms` });
    else if ((m = l.match(/^\[2\] inject (\S+) on '([^']+)'/))) { phase = 1; fault = m[1]; target = m[2]; once("inj", { i: "💥", c: "bad", t: `Injecting fault: ${plainName(fault, target)}` }); }
    else if ((m = l.match(/^intent #(\d+) running/)) && (intent = m[1])) once("int", { i: "🎯", t: `Fault is live (intent #${m[1]}); automatic rollback armed` });
    else if (l.startsWith("controller refused")) once("ref", { i: "⛔", c: "bad", t: l });
    else if ((m = l.match(/^\+\s*(\d+)s\s+err=\s*([\d.]+)%.*?down=\[([^\]]*)\](?:\s+failing=\[([^\]]*)\])?(?:\s+slow=\[([^\]]*)\])?/))) {
      const t = m[1], e = parseFloat(m[2]), down = m[3].replace(/'/g, "").trim(), failing = (m[4] || "").replace(/'/g, "").trim(), slow = (m[5] || "").replace(/'/g, "").trim();
      err = e;
      if (phase <= 1) {
        if (down) once("down", { i: "🔴", c: "bad", t: `${down.replace(/robotshop-|-\d+/g, "")} is DOWN (+${t} s)` });
        if (failing) once("fail", { i: "⚠️", c: "warn", t: `Shopper requests failing: ${failing} (+${t} s)` });
        if (slow) once("slow", { i: "🐢", c: "warn", t: `Requests slowing down: ${slow} (+${t} s)` });
        if (e >= 5) once("err5", { i: "📈", c: "bad", t: `Error rate up to ${e.toFixed(1)}% (+${t} s)` });
      }
    }
    else if (l.startsWith("no breach detected")) once("nob", { i: "ℹ️", t: "No breach: the system absorbed this fault" });
    else if (l.startsWith("[4] rollback")) { phase = Math.max(phase, 3); once("rb", { i: "🛑", c: "warn", t: "Problem detected → rolling the fault back automatically" + (l.includes("restart web") ? " (and restarting the shop gateway)" : "") }); }
    else if ((m = l.match(/\[auto-heal\] (\S+) stayed down/))) once("heal" + m[1], { i: "🩹", c: "warn", t: `${m[1].replace(/robotshop-|-\d+/g, "")} crashed as a side effect and did not come back by itself → restarted automatically (finding)` });
    else if ((m = l.match(/\[autopilot\] auto-heal: (\S+)/))) once("aheal" + m[1], { i: "🩹", c: "warn", t: `${m[1].replace(/robotshop-|-\d+/g, "")} was still down → autopilot restarted it so the campaign can continue` });
    else if (l.includes("already rolled back by the controller")) once("ks", { i: "🛡️", t: "The controller's kill switch had already rolled it back" });
    else if (l.startsWith("[5] verifying recovery")) once("ver", { i: "🔄", t: "Checking that the shop recovers" });
    else if ((m = l.match(/^evidence\s*:\s*(.*)/))) once("evi", { i: "🧾", t: `Proof recorded: ${m[1].replace(" via the shop ->", " →")}` });
    else if ((m = l.match(/^time to detect\s*:\s*(.*)/))) detect = m[1];
    else if ((m = l.match(/^time to recover\s*:\s*(.*)/))) rec = m[1];
    else if ((m = l.match(/^recovered\s*:\s*(\w+)/))) {
      phase = 4;
      once("rec", m[1] === "True" ? { i: "✅", c: "good", t: `Recovered: detected in ${detect || "-"}, back to normal ${rec || ""}` }
                                  : { i: "❌", c: "bad", t: "Did not fully recover within the wait window" });
    }
  }
  if (phase === 1 && ev.some((e) => e.c === "bad" && e.i !== "💥")) phase = 2;   // impact seen = problem detected
  return { ev, phase, err, fault, target, intent };
}
function renderFeed(lines, title, running, hint) {
  const r = parseRun(lines);
  $("now-title").textContent = title || (r.fault ? plainName(r.fault, r.target) : "Waiting to start");
  $("now-err").innerHTML = running && r.err != null && r.phase >= 1 && r.phase < 4 ? `live error rate <b class="${r.err >= 5 ? "worse" : ""}">${r.err.toFixed(1)}%</b>` : "";
  $("now-phases").innerHTML = PHASES.map((p, i) => `<li class="${i < r.phase || (i === r.phase && i === 4) ? "done" : i === r.phase ? (running ? "cur" : "done") : ""}">${p}</li>`).join("");
  const seen = state.feedSeen || (state.feedSeen = {}), nowT = new Date();
  const key = (e) => (r.intent || r.fault || "") + "|" + e.t;
  const fresh = r.ev.filter((e) => !seen[key(e)]);
  fresh.forEach((e) => { seen[key(e)] = state.feedPrimed ? nowT : null; });
  const feed = $("ap-feed"), atBottom = feed.scrollTop + feed.clientHeight >= feed.scrollHeight - 8;
  feed.innerHTML = r.ev.length ? r.ev.map((e) => { const t = seen[key(e)];
      return `<li class="${e.c || ""} ${t && nowT - t < 2500 ? "new" : ""}"><span class="fi">${e.i}</span><span>${esc(e.t)}</span><span class="ft">${t ? t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : ""}</span></li>`; }).join("")
    : '<li class="muted empty">Events appear here as the autopilot works: each fault, what broke, the rollback and the recovery.</li>';
  if (fresh.length && (atBottom || running)) feed.scrollTop = feed.scrollHeight;
  state.feedPrimed = true;
  $("ap-now").dataset.phase = r.phase;
  state.run = { phase: r.phase, target: baseNode(r.target || hint) || null, running };
  paintTopology();
}

/* ================================================================== AUTOPILOT */
const STEP_LABEL = { queued: "Queued", waiting: "Waiting for a healthy system", running: "Running…", done: "✅ Done", failed: "❌ Failed", skipped: "Skipped", aborted: "Stopped" };
function renderAutopilot(st) {
  state.ap = st;
  const steps = st.state === "idle" ? (st.preview || []).map((c, i) => ({ n: i + 1, ...c, status: "planned" })) : st.steps;
  $("ap-steps").innerHTML = steps.map((s) => {
    const r = s.result || {};
    const res = s.status === "done" || s.status === "failed"
      ? [r.time_to_detect && !r.time_to_detect.startsWith("None") ? `detected in ${r.time_to_detect}` : "no breach detected",
         r.time_to_recover && !r.time_to_recover.startsWith("None") ? `recovered ${r.time_to_recover.replace(/ after rollback$/, "")} after rollback` : r.recovered === "False" ? "did not recover by itself" : "",
         r["auto-healed"] ? `auto-healed: ${r["auto-healed"].replace(/robotshop-|-\d+|\s*\(.*$/g, "")}` : "",
         r.evidence ? `proof: ${r.evidence.replace(/^GET /, "").replace(" via the shop ->", " →")}` : ""].filter(Boolean).join(" · ")
      : s.status === "waiting" && s.waiting_for ? `waiting for: ${s.waiting_for}` : "";
    const prog = s.status === "running" && s.started ? `<div class="st-prog"><i data-since="${s.started}" data-expect="110"></i></div>
      <div class="st-why">running for <span data-since="${s.started}">0 s</span></div>` : "";
    return `<li class="step ${s.status} ${state.selStep === s.n ? "sel" : ""}" data-target="${esc(baseNode(s.target))}" data-n="${s.n}"><span class="num">${s.status === "done" ? "✓" : s.status === "failed" ? "!" : s.n}</span>
      <div><div class="st-name">${esc(plainName(s.fault, s.target, s.params))} <span class="st-chip ${s.status}">${s.status === "planned" ? "Planned" : STEP_LABEL[s.status] || s.status}</span></div>
      <div class="st-why">${res ? esc(res) : esc(s.reason || "")}</div>${prog}</div></li>`;
  }).join("");
  $("ap-steps").querySelectorAll(".step").forEach((li) => {
    li.addEventListener("mouseenter", () => setHighlight(li.dataset.target, "step"));
    li.addEventListener("mouseleave", () => setHighlight(null));
    li.addEventListener("click", () => { state.selStep = +li.dataset.n; state.selNode = li.dataset.target; paintTopology(); renderNodeDetail();
      $("ap-steps").querySelectorAll(".step").forEach((x) => x.classList.toggle("sel", x === li)); });
  });
  tick();
  const done = st.state === "done", running = ["running", "reporting"].includes(st.state);
  $("ap-start").disabled = running;
  $("ap-start").textContent = running ? (st.state === "reporting" ? "Writing the report…" : "Autopilot running…") : done ? "▶ Run autopilot again" : "▶ Start autopilot";
  $("ap-note").innerHTML = st.state === "idle" ? "Planned by the AI from the dependency map. Takes about 8–10 minutes; each fault is rolled back automatically."
    : done ? `✅ Campaign finished. ${st.report && st.report.ok ? `Findings report ready (resilience ${st.report.overall_resilience}/10): <a class="dl" href="#report">open AI report</a>` : "Report could not be generated: " + esc((st.report || {}).error || "")}`
    : st.state === "aborted" ? "⏹ Stopped by emergency stop; every active fault was rolled back."
    : st.state === "reporting" ? "All experiments done; the AI agents are writing the findings report…" : "";
  const cur = (st.steps || []).find((x) => ["waiting", "running"].includes(x.status));
  const title = running && cur ? `Step ${cur.n} of ${st.steps.length}: ${plainName(cur.fault, cur.target, cur.params)}${cur.status === "waiting" ? " (waiting for a healthy system)" : ""}`
    : done ? "Campaign finished" : st.state === "interrupted" ? "Last campaign was interrupted (dashboard restarted)" : null;
  if (st.output && st.output.length) {
    renderFeed(st.output, title, running, cur && cur.target);
    const log = $("ctl-log"), atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
    log.textContent = st.output.join("\n");
    if (atBottom) log.scrollTop = log.scrollHeight;
  } else if (title) { $("now-title").textContent = title; }
  else if (!state.ctl.hasJob) {   // nothing has run since the dashboard started: clean slate
    $("now-title").textContent = st.state === "idle" ? `Ready: ${steps.length} experiments planned. Press Start autopilot.` : "Waiting to start";
    $("now-err").innerHTML = ""; $("now-phases").innerHTML = PHASES.map((x) => `<li>${x}</li>`).join("");
    $("ap-now").dataset.phase = -1;
    state.run = null;
  }
  if (running && state.apWas !== "running") $("log-details").open = true;
  if (state.apWas === "running" && st.state !== "running") { loadOverview(); state.reportLoaded = false; }
  state.apWas = st.state;
}
async function loadAutopilot() { try { renderAutopilot(await api("/api/autopilot/status")); } catch (e) { $("ap-note").textContent = "Autopilot unavailable: " + e.message; } }
// Two-click confirmation inside the page: first click arms the button for 5 s, second click acts.
function armed(btn, text) {
  if (btn.dataset.armed) { clearTimeout(+btn.dataset.armed); delete btn.dataset.armed; btn.classList.remove("arming"); return true; }
  btn.dataset.label = btn.textContent; btn.textContent = text; btn.classList.add("arming");
  btn.dataset.armed = setTimeout(() => { delete btn.dataset.armed; btn.classList.remove("arming"); btn.textContent = btn.dataset.label; }, 5000);
  return false;
}
async function startAutopilot() {
  const b = $("ap-start");
  if (!armed(b, "Click again to start (breaks 4 services, auto-rollback)")) return;
  b.disabled = true; b.textContent = "Starting…";
  try {
    const r = await fetch("/api/autopilot/start", { method: "POST" });
    if (!r.ok) { const d = await r.json().catch(() => ({})); toast("⛔", "Autopilot not started: " + (d.detail || r.status), "bad"); }
    else { toast("▶", "Autopilot started: the AI is running 4 experiments", "warn"); state.feedSeen = {}; state.feedPrimed = true; }
  } catch (e) { toast("⛔", "Autopilot not started: " + e.message, "bad"); }
  loadAutopilot();
}

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
    ks.controller ? ["Kill switch (controller)", ks.controller.state || "?",
      ks.controller.state === "TRIPPED" ? (ks.controller.reason || "rolled back") : ks.controller.is_safe_to_inject ? "safe to inject" : (ks.controller.safety_reason || ""),
      ks.controller.state === "TRIPPED" ? "alert" : ["WARNING_WINDOW_1", "ARMED", "RECOVERING"].includes(ks.controller.state) ? "warn" : ""] :
    ["Kill switch", ks.tripped ? "TRIPPED" : ks.breach_windows ? `BREACH ${ks.breach_windows}/${ks.consecutive_breach_windows}` : "ARMED",
      `error > ${pct(ks.max_error_rate, 0)}${ks.p95_latency_threshold_ms ? " or p95 > " + num(ks.p95_latency_threshold_ms, 0) + " ms" : ""} for ${ks.consecutive_breach_windows} windows`,
      ks.tripped ? "alert" : ks.breach_windows ? "warn" : ""],
    ["Active faults", faults, faults ? [...L.active_faults.map((a) => `${a.fault} → ${a.target}`), ...L.active_toxics.map((t) => `${t.toxic} on ${t.proxy}`)].join(", ") : "none injected", faults ? "alert" : ""],
  ].map(tile).join("");

  renderBanner(); renderTopology(); renderVboard(); renderLiveCharts(); renderProof(); renderSpark(); notifyChanges();
  $("live-tiles").querySelectorAll(".tile").forEach((t, i) => {
    const v = t.querySelector(".v").textContent, prev = (state.prevTiles || [])[i];
    if (prev != null && prev !== v) { t.classList.remove("changed"); void t.offsetWidth; t.classList.add("changed"); }
  });
  state.prevTiles = [...$("live-tiles").querySelectorAll(".tile .v")].map((x) => x.textContent);
}

// Small live error-rate chart inside the autopilot card, with the kill-switch threshold.
function renderSpark() {
  const h = state.live.history.slice(-40), thr = state.live.kill_switch.max_error_rate * 100;
  const labels = h.map((p) => new Date(p.ts * 1000).toLocaleTimeString([], { minute: "2-digit", second: "2-digit" }));
  const data = h.map((p) => p.err * 100), last = data[data.length - 1] || 0;
  const color = last >= thr ? css("--critical") : css("--good");
  const ch = state.charts["c-now"];
  if (ch && ch.$theme === document.documentElement.dataset.theme) {
    ch.data.labels = labels; ch.data.datasets[0].data = data; ch.data.datasets[0].borderColor = color; ch.data.datasets[0].backgroundColor = color + "22";
    ch.data.datasets[1].data = data.map(() => thr); ch.update("none");
  } else {
    draw("c-now", { type: "line", data: { labels, datasets: [
      { label: "Error rate (%)", data, borderColor: color, backgroundColor: color + "22", fill: true, borderWidth: 2, pointRadius: 0, tension: 0.3 },
      { label: "Kill-switch threshold", data: data.map(() => thr), borderColor: css("--critical"), borderDash: [4, 4], borderWidth: 1, pointRadius: 0 }] },
      options: { animation: false, responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: { enabled: true } },
        scales: { x: { display: false }, y: { beginAtZero: true, suggestedMax: thr * 2, ticks: { callback: (v) => v + "%", maxTicksLimit: 3, color: css("--muted"), font: { size: 10 } }, grid: { color: css("--grid") } } } } });
    state.charts["c-now"].$theme = document.documentElement.dataset.theme;
  }
  $("now-spark-v").innerHTML = `errors now <b class="${last >= thr ? "worse" : "better"}">${last.toFixed(1)}%</b> <span class="muted">(rollback at ${thr.toFixed(0)}%)</span>`;
}

function toast(icon, text, cls) {
  const el = document.createElement("div");
  el.className = "toast " + (cls || "");
  el.innerHTML = `<span>${icon}</span><span>${esc(text)}</span>`;
  $("toasts").appendChild(el);
  setTimeout(() => el.classList.add("out"), 5200);
  setTimeout(() => el.remove(), 5800);
}
// Pop a notification whenever a service goes down / comes back or a fault is injected / rolled back.
function notifyChanges() {
  const L = state.live, prev = state.prevLive;
  const faults = [...L.active_faults.map((f) => plainName(f.fault, f.target)), ...L.active_toxics.map((t) => plainName("network_latency", t.proxy))];
  const failing = Object.keys(L.services).filter((n) => L.services[n].failing);
  if (prev) {
    faults.filter((f) => !prev.faults.includes(f)).forEach((f) => toast("💥", "Fault injected: " + f, "bad"));
    prev.faults.filter((f) => !faults.includes(f)).forEach((f) => toast("🛑", "Fault rolled back: " + f, "warn"));
    failing.filter((n) => !prev.failing.includes(n)).forEach((n) => toast("🔴", `${cap(n)} is DOWN`, "bad"));
    prev.failing.filter((n) => !failing.includes(n)).forEach((n) => {
      const o = L.services[n].last_outage;
      toast("✅", `${cap(n)} recovered${o && o.end ? ` after ${Math.round(o.end - o.start)} s` : ""}`, "good");
    });
  }
  state.prevLive = { faults, failing };
}
// Once a second: progress bars and "for N s" counters (no server round trip).
function tick() {
  const now = Date.now() / 1000;
  document.querySelectorAll("[data-since]").forEach((el) => {
    const dt = Math.max(0, now - +el.dataset.since);
    if (el.tagName === "I") el.style.width = Math.min(100, (dt / +(el.dataset.expect || 100)) * 100) + "%";
    else el.textContent = dt < 90 ? `${Math.round(dt)} s` : `${Math.floor(dt / 60)} min ${Math.round(dt % 60)} s`;
  });
  if (state.live) paintTopology();
}

function renderProof() {
  const svc = state.live.services, order = ["web", "catalogue", "user", "cart", "shipping", "ratings", "payment"];
  const target = new Set(state.live.active_faults.map((f) => f.target));
  $("proof").innerHTML = `<tr><th>Service</th><th>Status</th><th>Container</th><th>Health check (via shop)</th><th class="num">Answer</th><th class="num">Time</th><th>Last outage</th></tr>` +
    order.map((n) => {
      const s = svc[n] || {}, pr = s.probe, c = s.container;
      const down = s.failing;
      const http = pr ? (pr.status != null ? pr.status : pr.error || "no answer") : "—";
      if (s.gateway_down && !down) return `<tr><td><b>${esc(n)}</b></td><td><span class="muted">● gateway down</span></td>
        <td>${c ? esc(c.state) : "—"}</td><td><code>${pr ? esc(pr.path) : "—"}</code></td><td class="num muted">—</td><td class="num">—</td><td>${outageText(s.last_outage)}</td></tr>`;
      const flip = state.prevProof && state.prevProof[n] !== !!down;
      return `<tr class="${down ? "proof-down" : ""} ${flip ? "flash" : ""}"><td><b>${esc(n)}</b>${target.has(n) || [...target].some((t) => String(t).startsWith(n + "_to_")) ? ' <span class="st CRITICAL">FAULT INJECTED</span>' : ""}</td>
        <td>${down ? '<span class="pill-bad">● DOWN</span>' : pr ? '<span class="pill-good">● UP</span>' : "—"}</td>
        <td>${c ? `${esc(c.state)}${c.health !== "n/a" ? " · " + esc(c.health) : ""}` : "—"}</td>
        <td><code>${pr ? esc(pr.path) : "—"}</code></td>
        <td class="num ${pr && !pr.ok ? "worse" : pr ? "better" : ""}"><b>${esc(http)}</b></td>
        <td class="num">${pr ? num(pr.ms, 0) + " ms" : "—"}</td>
        <td>${outageText(s.last_outage)}</td></tr>`;
    }).join("");
  state.prevProof = Object.fromEntries(order.map((n) => [n, !!(svc[n] || {}).failing]));
  renderRecordedProof();
}
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
function outageText(o) {
  if (!o) return '<span class="muted">none seen</span>';
  const why = [o.probe && !o.probe.ok ? `${esc(o.probe.path)} → <b>${esc(o.probe.status ?? o.probe.error)}</b>` : "", o.container && o.container !== "running" ? `container <b>${esc(o.container)}</b>` : ""].filter(Boolean).join(", ");
  if (o.end == null) return `<span class="worse"><b>DOWN</b> since ${clock(o.start)} (${Math.round(Date.now() / 1000 - o.start)} s)</span>${why ? ": " + why : ""}`;
  return `${clock(o.start)}–${clock(o.end)} (${Math.round(o.end - o.start)} s)${why ? ": " + why : ""}`;
}
function renderRecordedProof() {
  if (!state.data) return;
  const ev = state.data.metrics.filter((m) => m.evidence).slice(-6).reverse();
  $("proof-recorded").innerHTML = ev.length ? "<b>Recorded proof from experiment runs</b> (saved in <code>runs/</code>, cited in the findings report):<ul>" +
    ev.map((m) => `<li>${esc(m.fault)} on <b>${esc(m.target)}</b> (${esc(new Date(m.started_at).toLocaleString())}): <code>GET ${esc(m.evidence.path)}</code> through the shop → <b>${esc(m.evidence.status ?? m.evidence.error)}</b> at +${m.evidence.t} s${(m.services_down || []).length ? `; container down: ${esc(m.services_down.join(", "))}` : ""}</li>`).join("") + "</ul>"
    : '<span class="hint">Proof of each outage is recorded in the run file by newer runs (health probe through the shop).</span>';
}

const TOPO = { W: 860, H: 360, nw: 104, nh: 48 };
// Everything that (transitively) calls n: what breaks if n fails.
function blastRadius(n) {
  const edges = (state.live && state.live.edges) || [], out = new Set(), todo = [n];
  while (todo.length) { const x = todo.pop(); edges.forEach(([c, d]) => { if (d === x && !out.has(c)) { out.add(c); todo.push(c); } }); }
  return out;
}
const baseNode = (t) => String(t || "").split("_to_")[0];
function setHighlight(n, why) {
  state.hl = n ? { n, cascade: blastRadius(n), why } : null;
  paintTopology();
}
function buildTopology() {
  const L = state.live, { W, H, nw, nh } = TOPO;
  const rows = [["gateway", 46], ["service", 180], ["data", 312]];
  const order = { service: ["catalogue", "ratings", "cart", "shipping", "user", "payment", "dispatch"], data: ["mongodb", "mysql", "redis", "rabbitmq", "payment-gateway"] };
  const pos = {};
  rows.forEach(([tier, y]) => {
    const names = order[tier] || L.tiers[tier];
    names.forEach((n, i) => { pos[n] = { x: (W / (names.length + 1)) * (i + 1), y, tier }; });
  });
  const edges = L.edges.filter(([a, b]) => pos[a] && pos[b]).map(([a, b]) => {
    const p = pos[a], q = pos[b];
    let d;
    if (p.tier === q.tier) {  // same-row call: arc above the row
      const mx = (p.x + q.x) / 2, dy = Math.min(48, 14 + Math.abs(p.x - q.x) / 6);
      d = `M${p.x},${p.y - nh / 2} Q${mx},${p.y - nh / 2 - dy} ${q.x},${q.y - nh / 2}`;
    } else d = `M${p.x},${p.y + nh / 2} C${p.x},${(p.y + q.y) / 2} ${q.x},${(p.y + q.y) / 2} ${q.x},${q.y - nh / 2}`;
    return `<path class="edge" data-a="${esc(a)}" data-b="${esc(b)}" d="${d}"/><path class="edge-flow" data-a="${esc(a)}" data-b="${esc(b)}" d="${d}"/>`;
  }).join("");
  const nodes = Object.entries(pos).map(([n, p]) => `<g class="node" data-n="${esc(n)}" transform="translate(${p.x - nw / 2},${p.y - nh / 2})"><title>${esc(n)}</title>
      <circle class="ripple" cx="${nw / 2}" cy="${nh / 2}" r="${nw / 2}"></circle>
      <rect width="${nw}" height="${nh}"></rect><text x="${nw / 2}" y="18">${esc(n === "payment-gateway" ? "pay-gateway" : n)}</text>
      <text class="sub" x="${nw / 2}" y="35"></text><text class="boom" x="${nw - 4}" y="2">💥</text></g>`).join("");
  $("topo").innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Service dependency map">${edges}${nodes}</svg>`;
  $("topo").querySelectorAll(".node").forEach((g) => {
    const n = g.dataset.n;
    g.addEventListener("click", () => { state.selNode = state.selNode === n ? null : n; paintTopology(); renderNodeDetail(); });
    g.addEventListener("mouseenter", () => setHighlight(n, "hover"));
    g.addEventListener("mouseleave", () => setHighlight(null));
  });
  state.topoKey = JSON.stringify([L.edges, L.tiers]);
}
function renderTopology() {
  if (!state.live) return;
  if (state.topoKey !== JSON.stringify([state.live.edges, state.live.tiers]) || !$("topo").querySelector("svg")) buildTopology();
  paintTopology();
  renderNodeDetail();
}
function paintTopology() {
  const L = state.live; if (!L || !$("topo").querySelector("svg")) return;
  const svc = L.services, hl = state.hl, now = Date.now() / 1000;
  const targets = new Set([...L.active_faults.map((a) => baseNode(a.target)), ...L.active_toxics.map((t) => baseNode(t.proxy))]);
  const bad = (n) => !!(svc[n] && svc[n].failing);
  const down = (n) => !!(svc[n] && svc[n].container && !svc[n].container.up);
  // the experiment in progress (from its log): colour the tested service and its blast radius by phase
  const run = state.run && state.run.target && (state.run.running || state.run.phase === 4) ? state.run : null;
  const rt = run ? run.target : null, ph = run ? run.phase : -1;
  if (rt && ph >= 1 && ph <= 2) targets.add(rt);
  const atRisk = rt && ph >= 1 && ph <= 3 ? blastRadius(rt) : new Set();
  const busy = targets.size > 0 || Object.keys(svc).some(bad) || !!run;
  $("topo").classList.toggle("busy", busy);
  $("topo").classList.toggle("hl", !!hl);
  $("topo").querySelectorAll(".node").forEach((g) => {
    const n = g.dataset.n, sv = svc[n] || {}, c = sv.container;
    g.classList.toggle("target", targets.has(n));
    g.classList.toggle("bad", bad(n));
    g.classList.toggle("ok", !bad(n) && !!c);
    g.classList.toggle("sel", state.selNode === n);
    g.classList.toggle("hl-src", !!hl && hl.n === n);
    g.classList.toggle("hl-hit", !!hl && hl.cascade.has(n));
    const isRt = n === rt;
    g.classList.toggle("ph-base", isRt && ph === 0);
    g.classList.toggle("ph-fault", isRt && (ph === 1 || ph === 2));
    g.classList.toggle("ph-recovering", isRt && ph === 3 && !bad(n));
    g.classList.toggle("ph-recovered", isRt && ph === 4);
    g.classList.toggle("at-risk", atRisk.has(n) && !bad(n));
    const o = sv.last_outage;
    g.querySelector(".sub").textContent = o && o.end == null ? `DOWN ${Math.round(now - o.start)} s`
      : isRt && ph === 0 ? "measuring baseline"
      : isRt && (ph === 1 || ph === 2) ? "FAULT INJECTED"
      : isRt && ph === 3 ? "recovering…"
      : isRt && ph === 4 ? "recovered ✓"
      : atRisk.has(n) && !bad(n) ? "at risk"
      : c ? (c.up ? (sv.live_error_rate ? pct(sv.live_error_rate, 0) + " err" : "up") : c.state) : "";
  });
  $("topo").querySelectorAll(".edge, .edge-flow").forEach((e) => {
    const a = e.dataset.a, b = e.dataset.b;
    e.classList.toggle("hot", bad(b) || targets.has(b) && bad(a));
    e.classList.toggle("broken", down(b));
    e.classList.toggle("risk", !!rt && ph >= 1 && ph <= 3 && (b === rt || atRisk.has(b)) && atRisk.has(a));
    e.classList.toggle("hl-path", !!hl && (hl.n === b || hl.cascade.has(b)) && (hl.cascade.has(a)));
  });
}

function renderNodeDetail() {
  const n = state.selNode;
  if (!state.live) return;
  if (!n) { $("node-detail").innerHTML = '<span class="hint">Live system map: green = up, red = failing, 💥 = fault injected here, moving dots = live traffic. Hover a service (or a step) to see what breaks if it fails; click for details.</span>'; return; }
  const s = state.live.services[n] || {};
  const v = (state.live.vulnerability.rows.find((r) => r.service === n)) || null;
  const callers = state.live.edges.filter(([, d]) => d === n).map(([c]) => c);
  const deps = state.live.edges.filter(([c]) => c === n).map(([, d]) => d);
  const c = s.container;
  $("node-detail").innerHTML = `<b>${esc(n)}</b> · ${c ? `container <b>${esc(c.state)}</b>${c.health !== "n/a" ? " (" + esc(c.health) + ")" : ""}` : "no container info"}
    ${s.live_requests != null ? ` · live error <b>${pct(s.live_error_rate)}</b> over ${s.live_requests} req` : ""}<br>
    Calls: ${esc(deps.join(", ") || "—")} · Called by: ${esc(callers.join(", ") || "—")}<br>
    ${v ? (v.score != null ? `H<sub>v</sub> <b>${v.score}</b> (${esc(v.status)}, ${v.runs} run${v.runs > 1 ? "s" : ""}) · cascade (${esc(v.cascade_source)}): ${esc(v.cascade.join(", ") || "none")}`
          : `Not tested yet · predicted cascade: ${esc(v.cascade.join(", ") || "none")}`) : ""}
    ${s.last_outage ? `<br>Last outage: ${outageText(s.last_outage)}` : ""}
    <div class="row" style="margin-top:6px"><button type="button" class="mini" data-ask="${esc(n)}">Ask the copilot about ${esc(n)}</button></div>`;
  const b = $("node-detail").querySelector("[data-ask]");
  if (b) b.addEventListener("click", () => ask(`What happens to the shop if ${n} fails, and how fragile is it?`));
}

function renderVboard() {
  const V = state.live.vulnerability;
  $("coverage").textContent = `${V.coverage.tested}/${V.coverage.total} services tested`;
  $("hv-formula").textContent = V.formula + ". Untested services get no score: an untested service is a coverage gap, not \"stable\". Cascade is measured from the endpoints that failed; for untested services it is predicted from the dependency map.";
  $("vboard").innerHTML = V.rows.map((r) => {
    const moved = state.prevRanks[r.service] != null && state.prevRanks[r.service] !== r.rank;
    const newlyFailing = r.live_failing && !state.prevLiveFail[r.service];
    return `<li class="${r.live_failing ? "live" : ""} ${moved || newlyFailing ? "flash" : ""}">
      <span class="rk">#${r.rank}</span>
      <div><div class="nm">${esc(r.service)} ${r.live_failing ? '<span class="st CRITICAL">LIVE FAILING</span>' : ""}</div>
        <div class="cas">${r.cascade_source === "measured" ? "measured cascade: " + esc(r.cascade.join(", "))
          : r.cascade_source === "contained" ? `no other service failed (callers: ${esc(r.cascade.join(", ") || "none")})`
          : r.cascade.length ? "would break: " + esc(r.cascade.join(", ")) + " (predicted)" : "no downstream callers"}</div>
        ${(r.experiments || []).length ? `<div class="exps">${r.experiments.map((e) => `<span class="exp" title="${e.runs} run(s)${e.fix_runs ? ", " + e.fix_runs + " after a fix" : ""}">${esc(e.fault)}${e.edge ? " → " + esc(e.edge.dependency) : ""} <b>${e.score}</b> <span class="st ${e.level}">${e.level}</span></span>`).join("")}</div>` : ""}
        ${r.score != null ? `<div class="bar"><i style="width:${Math.min(100, r.score)}%"></i></div>` : ""}</div>
      <div class="sc">${r.score != null ? `<b>${r.score}</b><span class="muted">/100</span>` : '<span class="muted">—</span>'}<br><span class="st ${r.status}">${r.status}</span></div></li>`;
  }).join("");
  V.rows.forEach((r) => { state.prevRanks[r.service] = r.rank; state.prevLiveFail[r.service] = r.live_failing; });
  $("vboard").querySelectorAll("li").forEach((li, i) => {
    const n = V.rows[i].service;
    li.title = "Click to show it on the system map";
    li.addEventListener("mouseenter", () => setHighlight(n, "board"));
    li.addEventListener("mouseleave", () => setHighlight(null));
    li.addEventListener("click", () => { state.selNode = n; paintTopology(); renderNodeDetail(); $("topo").scrollIntoView({ behavior: "smooth", block: "center" }); });
  });
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

function defaultKpi(g) {
  const anyErr = g.trend.some((t) => (t.error_minutes || 0) > 0.0005 || (t.peak_error_rate || 0) > 0);
  const anySlow = g.trend.some((t) => (t.slowest_endpoint_ms || 0) > 0);
  return !anyErr && anySlow ? "slowest_endpoint_ms" : "error_minutes";
}
function renderGroups() {
  const sel = $("group"), prev = sel.value;
  sel.innerHTML = state.data.groups.map((g, i) => `<option value="${i}">${esc(g.fault)} → ${esc(g.target)} (${g.runs.length} run${g.runs.length === 1 ? "" : "s"})</option>`).join("");
  if (prev && +prev < state.data.groups.length) sel.value = prev;
  state.groupIdx = +sel.value || 0;
  if (!prev && state.data.groups[state.groupIdx]) $("trend-kpi").value = defaultKpi(state.data.groups[state.groupIdx]);
  renderTrend();
}

const BASE_PHASE = "baseline (no change)";
const fmtP = (p) => (p == null ? "n/a" : p < 0.000001 ? "< 0.000001" : String(p));
const rangeTxt = (a, b) => (a === b ? `Run ${a}` : `Runs ${a}–${b}`);
const runIn = (g, id) => "Run " + (g.runs.indexOf(id) + 1);  // numbering within this experiment

// Shaded run-to-run noise band over the unchanged baseline runs + a divider where each fix starts.
function phasePlugin(g, kpi, label = true) {
  return { id: "phases", beforeDatasetsDraw(chart) {
    const { ctx, chartArea: a, scales: { x, y } } = chart;
    const idx = g.trend.map((t, i) => [t, i]);
    const step = g.trend.length > 1 ? x.getPixelForValue(1) - x.getPixelForValue(0) : a.right - a.left;
    ctx.save();
    const base = idx.filter(([t]) => t.phase === BASE_PHASE);
    const vals = base.map(([t]) => t[kpi]).filter((v) => v != null);
    if (base.length >= 2 && vals.length >= 2 && Math.max(...vals) > 0) {
      const x0 = x.getPixelForValue(base[0][1]) - step / 2, x1 = x.getPixelForValue(base[base.length - 1][1]) + step / 2;
      const yTop = y.getPixelForValue(Math.max(...vals)), yBot = y.getPixelForValue(Math.min(...vals));
      ctx.fillStyle = css("--noise-band"); ctx.fillRect(x0, yTop, x1 - x0, Math.max(2, yBot - yTop));
      if (label) {
        ctx.fillStyle = css("--muted"); ctx.font = "11px system-ui, sans-serif"; ctx.textAlign = "right";
        ctx.fillText("noise band (no change)", x1 - 4, Math.max(a.top + 11, yTop - 4));
      }
    }
    for (let i = 1; i < g.trend.length; i++) {
      if (g.trend[i].phase === g.trend[i - 1].phase) continue;
      const px = (x.getPixelForValue(i - 1) + x.getPixelForValue(i)) / 2;
      ctx.strokeStyle = css("--ink-2"); ctx.setLineDash([4, 4]); ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = css("--ink"); ctx.font = "600 11px system-ui, sans-serif"; ctx.textAlign = "left";
      ctx.fillText(g.trend[i].phase.startsWith("fix removed") ? "▸ fix removed" : "▸ fix applied", px + 5, a.top + 11);
    }
    ctx.restore();
  },
  afterDatasetsDraw(chart) {  // value labels on the fix-phase bars (they can be too small to see)
    const { ctx } = chart;
    ctx.save(); ctx.fillStyle = css("--ink"); ctx.font = "600 11px system-ui, sans-serif"; ctx.textAlign = "center";
    chart.data.datasets.forEach((ds, di) => {
      if (ds.label === BASE_PHASE || ds.label === "Time to detect" || ds.label === "Time to recover") return;
      chart.getDatasetMeta(di).data.forEach((el, i) => {
        const v = ds.data[i];
        if (v == null) return;
        ctx.fillText(kpi.endsWith("_rate") ? pct(v, 1) : num(v, 3), el.x, el.y - 5);  // compact: bars are narrow
      });
    });
    ctx.restore();
  } };
}

function trendFindings(g) {
  const li = (icon, cls, html) => `<li class="${cls}"><span class="fi">${icon}</span><span>${html}</span></li>`;
  const out = [], name = plainName(g.fault, g.target, (g.trend[0] || {}).params);
  for (const e of g.effects) {
    const em = e.deltas.error_minutes, sc = e.deltas.resilience_score;
    const by = em.old != null && Math.abs(em.old) < 0.005 ? `from ${num(em.old, 3)} to ${num(em.new, 3)} error-minutes` : `by ${pct(Math.abs(e.primary_relative_change || 0), 1)}`;
    if (e.to_phase.startsWith("fix removed")) {
      out.push(e.verdict === "regressed" ? li("❌", "bad", `<b>Removing the fix brought the errors back</b> (${by}). Proven, not chance.`)
        : li("ℹ️", "", `Removing the fix: no clear change (could be chance).`));
    } else if (e.verdict === "improved") {
      out.push(li("✅", "good", `<b>The fix worked:</b> “${esc(e.to_phase.replace(/^fix:\s*/, ""))}” cut errors ${by}. Proven, not chance.`));
      if (sc && sc.better === false) out.push(li("⚠️", "warn", `<b>Side effect:</b> during failover some requests hung for up to a minute, so the overall score fell (${num(sc.old, 1)} → ${num(sc.new, 1)}/10). Next fix: a short timeout in the gateway.`));
    } else if (e.verdict === "regressed") {
      out.push(li("❌", "bad", `<b>“${esc(e.to_phase)}” made it worse</b> (${by}). Proven, not chance.`));
    } else {
      out.push(li("ℹ️", "", `“${esc(e.to_phase)}”: no clear change; the difference could be chance.`));
    }
  }
  const base = g.phases.find((ph) => ph.phase === BASE_PHASE);
  if (base && base.n_runs > 1)
    out.push(li("ℹ️", "", `${base.n_runs} repeats of the same test with nothing changed: results vary naturally (the grey band), so they don't trend.`));
  if (defaultKpi(g) === "slowest_endpoint_ms") {
    const peak = Math.max(...g.trend.map((t) => t.slowest_endpoint_ms || 0));
    out.push(li("🐢", "warn", `<b>${esc(name)}</b> causes slowdowns, not errors: the slowest request peaked at ${num(peak / 1000, 1)} s.`));
  }
  if (!g.effects.length) out.push(li("➡️", "", `To prove a fix, apply it and re-run this experiment (Autopilot re-runs it automatically).`));
  return out.join("");
}

function trendCaption(g, kpi = "error_minutes") {
  const n = g.trend.length, first = {};
  g.trend.forEach((t, i) => { if (!(t.phase in first)) first[t.phase] = i; });
  const parts = [];
  const base = g.phases.find((p) => p.phase === BASE_PHASE);
  if (base && base.n_runs > 1) {
    const b0 = first[BASE_PHASE] + 1;
    const vals = g.trend.filter((t) => t.phase === BASE_PHASE).map((t) => t[kpi]).filter((v) => v != null);
    const lo = Math.min(...vals), hi = Math.max(...vals);
    parts.push(`${rangeTxt(b0, b0 + base.n_runs - 1)} repeated the same experiment with no change: their spread ` +
      `(${fmtKpi(kpi, lo)} to ${fmtKpi(kpi, hi)} ${KPI_LABEL[kpi].toLowerCase()}) is run-to-run noise, so they do not trend.`);
  }
  for (const e of g.effects) {
    const ch = e.primary_relative_change;
    const what = e.verdict === "improved" ? "a significant improvement" : e.verdict === "regressed" ? "a significant regression"
      : e.verdict === "mixed" ? "mixed signals (not claimed)" : "not significant (not claimed)";
    const em = e.deltas.error_minutes;
    const moved = em.old != null && em.new != null && Math.abs(em.old) < 0.005
      ? `${em.new >= em.old ? "+" : "−"}${num(Math.abs(em.new - em.old), 3)} min (${num(em.old, 3)} → ${num(em.new, 3)})` : signedChange(ch);
    parts.push(`After “${e.to_phase}” (${e.to_runs} run${e.to_runs > 1 ? "s" : ""}) error-minutes moved ${moved} ` +
      `vs ${e.from_phase === BASE_PHASE ? "the baseline" : "“" + e.from_phase + "”"} (p ${fmtP(e.significance.p_value).startsWith("<") ? "" : "= "}${fmtP(e.significance.p_value)}): ${what}.`);
    const sc = e.deltas.resilience_score;
    if (e.verdict === "improved" && sc && sc.better === false)
      parts.push(`The resilience score still fell (${num(sc.old, 2)} → ${num(sc.new, 2)}/10) because it also counts latency: requests ` +
        `routed to the crashed replica hung until failover, so a short gateway connect timeout is the next fix.`);
  }
  if (n === 1) parts.push("One run so far: apply a fix and re-run with --label \"fix: …\" (the AI report gives the exact command) to measure the improvement.");
  if (!g.effects.length && n > 1) parts.push("No fix has been applied to this experiment yet: apply one, then re-run with --label \"fix: …\" to measure it.");
  return parts.join(" ");
}

function renderTrend() {
  const g = state.data.groups[state.groupIdx];
  if (!g) { $("steps").innerHTML = `<tr><td class="empty">No runs yet.</td></tr>`; return; }
  const kpi = $("trend-kpi").value, fmt = (v) => fmtKpi(kpi, v);
  const labels = g.trend.map((t) => runIn(g, t.run_id) + (t.phase.startsWith("fix removed") ? " · no fix" : t.phase !== BASE_PHASE ? " · fix" : ""));
  const phases = [...new Set(g.trend.map((t) => t.phase))];
  const colors = [css("--s1"), css("--s2"), css("--s3")];
  const colorOf = (ph) => colors[phases.indexOf(ph) % colors.length];
  const o1 = baseOptions(fmt, KPI_LABEL[kpi] + " per run");
  o1.plugins.legend = { display: phases.length > 1, labels: o1.plugins.legend.labels };
  o1.plugins.tooltip = { callbacks: { title: (it) => { const t = g.trend[it[0].dataIndex]; return `${runIn(g, t.run_id)} (${t.run_id}) · ${t.label || BASE_PHASE}`; },
                                      label: (c) => `${KPI_LABEL[kpi]}: ${fmt(c.parsed.y)}` } };
  draw("trend", { type: "bar", plugins: [phasePlugin(g, kpi)], data: { labels, datasets: phases.map((ph) =>
    bar(ph, g.trend.map((t) => (t.phase === ph ? t[kpi] : null)), colorOf(ph), { skipNull: true })) }, options: o1 });
  const sfmt = (v) => num(v) + " s";
  draw("times", { type: "bar", plugins: [phasePlugin(g, "time_to_detect_s", false)], data: { labels, datasets: [
    bar("Time to detect", g.trend.map((t) => t.time_to_detect_s), css("--s1")),
    bar("Time to recover", g.trend.map((t) => t.time_to_recover_s), css("--s2"))] }, options: baseOptions(sfmt, "Detection & recovery (s)") });
  const noErrors = defaultKpi(g) === "slowest_endpoint_ms";
  $("trend-findings").innerHTML = trendFindings(g);
  $("trend-caption").textContent = (noErrors ? `This fault causes slowdowns, not errors (0 failed requests in every run), so the chart shows ${KPI_LABEL[kpi === "slowest_endpoint_ms" ? kpi : "slowest_endpoint_ms"].toLowerCase()}: the peak per-window mean of the slowest endpoint. ` : "")
    + (kpi === "error_minutes" && noErrors ? "Error-minutes are 0 for every run. " : "") + trendCaption(g, kpi);

  const cell = (dl) => dl.old == null && dl.new == null ? `<td class="num muted" title="not measured by these runs">—</td>`
    : `<td class="num ${dl.better === true ? "better" : dl.better === false ? "worse" : ""}" title="${esc(dl.note || "")}">${esc(dl.old == null ? "—" : fmtKpi(dl.kpi, dl.old))} → ${esc(dl.new == null ? "—" : fmtKpi(dl.kpi, dl.new))}</td>`;
  // a % change of a near-zero baseline is meaningless (0.001 -> 0.016 = "+1500%"): show the absolute change instead
  const change = (s) => {
    const em = s.deltas.error_minutes, rel = s.primary_relative_change;
    if (em.old != null && em.new != null && Math.abs(em.old) < 0.005)
      return { txt: `${em.new - em.old >= 0 ? "+" : "−"}${num(Math.abs(em.new - em.old), 3)} min`, cls: em.new < em.old ? "better" : em.new > em.old ? "worse" : "" };
    return { txt: signedChange(rel), cls: rel > 0 ? "better" : rel < 0 ? "worse" : "" };
  };
  const row = (s, title, sub, cls = "") => `<tr class="${cls}"><td>${title}${sub ? `<br><span class="hint">${esc(sub)}</span>` : ""}</td>
      <td>${badge(s.verdict)}</td>${cell(s.deltas.error_minutes)}
      <td class="num ${change(s).cls}">${change(s).txt}</td>
      ${cell(s.deltas.fault_error_rate)}${cell(s.deltas.slowest_endpoint_ms)}<td class="num">${fmtP(s.significance.p_value)}</td>
      ${cell(s.deltas.time_to_detect_s)}${cell(s.deltas.time_to_recover_s)}${cell(s.deltas.resilience_score)}</tr>`;
  const head = `<tr><th>Comparison</th><th>Verdict</th><th class="num">Error-minutes</th><th class="num">Change</th><th class="num">Fault error rate</th><th class="num">Slowest endpoint</th><th class="num">p-value</th><th class="num">Detect</th><th class="num">Recover</th><th class="num">Score</th></tr>`;
  const effects = g.effects.map((e) => row(e, `<b>${e.to_phase.startsWith("fix removed") ? "Fix removed" : "Before vs after fix"}</b>`,
    `${e.from_phase} (${e.from_runs} run${e.from_runs > 1 ? "s" : ""}) → ${e.to_phase} (${e.to_runs} run${e.to_runs > 1 ? "s" : ""}), runs pooled`, "fix-row"));
  const phaseOf = Object.fromEntries(g.trend.map((t) => [t.run_id, t.phase]));
  const steps = g.steps.filter((s) => phaseOf[s.from] === phaseOf[s.to])
    .map((s) => row(s, esc(runIn(g, s.from) + " → " + runIn(g, s.to)), "same setup, repeated"));
  const cum = g.cumulative && !g.effects.length ? [row(g.cumulative, "<b>First → latest</b>", "")] : [];  // phase rows cover it
  $("steps").innerHTML = head + ([...effects, ...steps, ...cum].join("") ||
    `<tr><td colspan="10" class="empty">Only one run of this experiment. Re-run it after a fix (with <code>--label "fix: …"</code>) to measure improvement.</td></tr>`);
  $("verdict-legend").innerHTML = `<b>Proven</b> = bigger than the natural variation between identical runs (p &lt; 0.05). <b>Noise</b> = could be chance, so not claimed. Highlighted rows compare all runs before vs after a change. <span class="muted">Details:</span> <span class="badge b-improved"><span class="dot"></span>significant</span> the change is bigger than the run-to-run noise of repeating the same experiment (two-proportion z-test on failed requests, p &lt; 0.05), so we claim it. <span class="badge b-none"><span class="dot"></span>not significant (noise)</span> the difference could be chance (too few failures to tell), so we do not claim it. Highlighted rows compare whole phases (all runs before vs after a change, pooled); the other rows repeat the same setup.`;
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
  if (w) {
    const wm = state.data.metrics.find((m) => m.run_id === w.run_id) || {};
    $("biz-banner-title").textContent = `Costliest failure: ${plainName(wm.fault, wm.target, wm.params)}, about ${money(w.cost_per_incident)} per incident`;
    $("biz-banner-sub").textContent = `≈ ${num(w.orders_lost, 0)} orders lost each time · ${money(w.annual_exposure)} a year at ${num(b.assumptions.incidents_per_year, 0)} incidents · projected from the measured outage with the assumptions below.`;
  } else { $("biz-banner-title").textContent = "Run an experiment to see its business cost"; $("biz-banner-sub").textContent = ""; }
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

async function loadBrief(ai = true) {   // AI brief; cached per run on disk, so re-opening costs nothing
  const id = $("brief-run").value; if (!id) return;
  state.briefFor = id;
  $("brief").innerHTML = `<span class="hint">${ai ? "Asking the AI to write the brief…" : "Writing brief…"}</span>`; $("brief-meta").textContent = "";
  renderFunnel();
  try {
    const r = await api("/api/brief/" + encodeURIComponent(id) + (ai ? "" : "?ai=false"), state.assumptions);
    if (state.briefFor !== id) return;
    $("brief").innerHTML = mdLite(r.brief).replace(/^(What happened:|What it affects:|Time and money:|Recommendation:)/gm, '<span class="lbl">$1</span>');
    $("brief-meta").textContent = r.mode === "llm" ? `Written by ${r.model} from the measured numbers (it is not allowed to invent figures).` : `Rule-based brief${r.note ? " · AI unavailable: " + r.note : ""}`;
  } catch (e) { $("brief").textContent = "Error: " + e.message; }
  if (ai) loadUsage();
}

/* ================================================================== AI REPORT (multi-agent) */
const AGENTS = [
  ["planner", "Ranks and selects chaos experiments from the dependency map"],
  ["analyzer", "Explains findings, ranks weaknesses by severity, scores resilience"],
  ["recommender", "Prioritised, verifiable fix per weakness"],
];
function renderAgents(traces, running) {
  const byName = Object.fromEntries((traces || []).map((t) => [t.agent, t]));
  $("rep-agents").innerHTML = AGENTS.map(([n, role], i) => {
    const t = byName[n];
    const cls = running ? "running" : t && t.mode === "llm" ? "done-llm" : "";
    return `<div class="agent ${cls}">${running ? "" : `<span class="step">${i + 1}/3</span>`}
      <div class="an">${esc(n)} agent</div><div class="ar">${esc(t ? t.role : role)}</div>
      <div class="am">${t ? `mode <b>${esc(t.mode === "llm" ? "AI" : "rules")}</b>${t.model ? " · " + esc(t.model) : ""} · ${t.duration_s} s` : running ? "" : "not run yet"}</div>
      ${t && t.notes && t.notes.length ? `<div class="am" style="margin-top:4px">${esc(t.notes.join("; "))}</div>` : ""}</div>`;
  }).join("");
}
function renderReport(r) {
  const cur = (r.business_assumptions && r.business_assumptions.currency) || "USD";
  const m = (x) => { try { return new Intl.NumberFormat(undefined, { style: "currency", currency: cur, maximumFractionDigits: 0 }).format(x); } catch { return cur + " " + num(x, 0); } };
  renderAgents(r.agents, false);
  $("rep-meta").innerHTML = `<span class="pill">generated ${esc(new Date(r.generated_at).toLocaleString())}</span>
    <span class="pill">${r.experiments_run} runs · ${r.distinct_experiments} experiments</span>
    <a class="dl" href="/api/report/download/md">Download FINDINGS_REPORT.md</a>
    <a class="dl" href="/api/report/download/json">Download JSON</a>`;
  const worst = r.findings[0];
  const totalExp = r.recommendations.reduce((s, x) => s + (x.protects.annual_exposure || 0), 0);
  $("rep-tiles").innerHTML = [
    ["Overall resilience", r.overall_resilience != null ? r.overall_resilience + "/10" : "–", "mean of per-experiment scores"],
    ["Experiments", r.distinct_experiments, `${r.failure_types.length} failure types: ${r.failure_types.join(", ")}`],
    ["Most severe", worst ? worst.severity : "–", worst ? `${worst.fault} on ${worst.target} (${worst.severity_score}/100)` : ""],
    ["Annual exposure covered", m(totalExp), "sum over recommended fixes (projected)"],
    ["Services tested", `${r.coverage.tested}/${r.coverage.total}`, r.coverage.untested.slice(0, 4).join(", ") + (r.coverage.untested.length > 4 ? "…" : "")],
  ].map(tile).join("");
  $("rep-findings").innerHTML = (r.summary ? `<p class="fx" style="margin-top:0">${mdLite(r.summary)}</p>` : "") + r.findings.map((f) => {
    const e = f.evidence, se = e.slowest_endpoint;
    return `<div class="finding"><div class="fh"><span class="ft">#${f.rank} · ${esc(f.fault)} on ${esc(f.target)}</span>
      <span><span class="sev ${f.severity}">${f.severity.toUpperCase()} ${f.severity_score}</span> <span class="chip">resilience ${f.resilience_score}/10</span></span></div>
      <div class="fx">${mdLite(f.explanation)}${f.explained_by === "ai" ? '<span class="ai-tag">AI</span>' : ""}</div>
      <div class="fe"><span class="chip">peak error ${pct(e.peak_error_rate)}</span><span class="chip">detect ${num(e.time_to_detect_s)} s</span>
        <span class="chip">recover ${num(e.time_to_recover_s)} s</span>${se ? `<span class="chip">${esc(se.endpoint)} ${num(se.peak_ms, 0)} ms (${se.ratio}x)</span>` : ""}
        ${(e.endpoints_failing || []).length ? `<span class="chip">failing: ${esc(e.endpoints_failing.join(", "))}</span>` : ""}
        ${f.run_to_run_verdict ? `<span class="chip">${f.runs} runs: ${esc(verdictText(f.run_to_run_verdict))}</span>` : ""}</div></div>`;
  }).join("");
  $("rep-recs").innerHTML = r.recommendations.map((x) => `<div class="finding"><div class="fh">
      <span class="ft">${x.priority}. ${esc(x.fix)} · ${esc(x.service)}</span>
      <span><span class="sev ${x.severity}">${x.severity.toUpperCase()}</span> <span class="chip">protects ${m(x.protects.annual_exposure || 0)}/yr${x.protects.estimated ? " (est.)" : ""}</span></span></div>
      <div class="fx">${mdLite(x.action)}${x.written_by === "ai" ? '<span class="ai-tag">AI</span>' : ""}</div>
      <div class="verify">verify: ${esc(x.verify)}</div></div>`).join("") || '<span class="hint">No weaknesses yet.</span>';
  renderPlan(r.plan);
}
function renderPlan(p) {
  if (!p) return;
  $("plan-method").textContent = `${p.method}. Budget ${p.budget} experiments, ${p.already_run} already run.`;
  const sel = new Set(p.selected.map((s) => s.id));
  $("plan-table").innerHTML = `<tr><th>#</th><th>Experiment</th><th class="num">Score</th><th>Status</th><th>Reason</th></tr>` +
    p.ranked.slice(0, 10).map((c, i) => `<tr><td>${i + 1}</td><td><code>${esc(c.id)}</code></td><td class="num">${c.score}</td>
      <td>${sel.has(c.id) ? '<span class="badge b-improved"><span class="dot"></span>selected</span>' : c.already_run ? '<span class="badge b-none"><span class="dot"></span>already run</span>' : ""}</td>
      <td>${esc(c.ai_reason || c.reason)}${c.ai_reason ? '<span class="ai-tag">AI</span>' : ""}</td></tr>`).join("");
}
async function generateReport() {
  const btn = $("rep-go");
  btn.disabled = true; btn.textContent = "Agents working…";
  renderAgents([], true);
  try {
    const r = await api("/api/report", { use_llm: $("rep-llm").checked, assumptions: state.assumptions });
    renderReport(r);
  } catch (e) {
    $("rep-meta").innerHTML = `<span class="pill">Error: ${esc(e.message)}</span>`;
    renderAgents([], false);
  } finally { btn.disabled = false; btn.textContent = "Generate report"; loadUsage(); }
}
async function loadReportTab() {
  try { renderReport(await api("/api/report/latest")); }
  catch { renderAgents([], false); try { renderPlan(await api("/api/plan")); } catch {} }
}

/* ================================================================== EXPERIMENT CONSOLE */
state.ctl = { menu: [], running: false, wasRunning: false };
function renderControl(c) {
  const s = c.safety || {};
  const pill = $("ctl-safety");
  pill.className = "pill " + (s.available ? (s.is_safe_to_inject ? "safe" : "unsafe") : "");
  pill.textContent = s.available ? (s.is_safe_to_inject ? `safe to inject · kill switch ${s.kill_switch_state}` : `not safe: ${s.reason || s.kill_switch_state}`)
    : "controller safety status unavailable";
  const sel = $("ctl-pick"), keep = sel.value;
  if (JSON.stringify(c.menu.map((m) => m.id)) !== JSON.stringify(state.ctl.menu.map((m) => m.id))) {
    const opt = (m) => `<option value="${esc(m.id)}">${c.recommended.includes(m.id) ? "★ " : ""}${esc(m.id)}${m.already_run ? "" : " · score " + m.score}</option>`;
    const rerun = c.menu.filter((m) => m.already_run), fresh = c.menu.filter((m) => !m.already_run);
    sel.innerHTML = (rerun.length ? `<optgroup label="Re-run a recorded experiment (e.g. after a fix)">${rerun.map(opt).join("")}</optgroup>` : "") +
      (fresh.length ? `<optgroup label="New experiments (planner order${c.budget && !c.budget.left ? ", experiment budget used" : ""})">${fresh.map(opt).join("")}</optgroup>` : "");
    sel.value = keep && c.menu.some((m) => m.id === keep) ? keep : (c.recommended[0] || (rerun[0] || fresh[0] || {}).id || "");
  }
  state.ctl.menu = c.menu;
  const pick = c.menu.find((m) => m.id === sel.value);
  const budgetNote = c.budget && !c.budget.left && pick && !pick.already_run
    ? ` Note: ${c.budget.used} of ${c.budget.max} planned experiments are already recorded; this would add another.` : "";
  $("ctl-reason").textContent = pick ? `${c.recommended.includes(pick.id) ? "Planner pick. " : ""}${pick.already_run ? "Re-run of a recorded experiment: compare it on the Experiments tab. " : ""}${pick.reason}${Object.keys(pick.params || {}).length ? " Params: " + JSON.stringify(pick.params) : ""}${budgetNote}` : "";
  const job = c.job, running = !!(job && job.running);
  state.ctl.hasJob = !!(job && (c.output || []).length);
  state.ctl.running = running;
  $("ctl-run").disabled = running || !$("ctl-approve").checked || (s.available && !s.is_safe_to_inject);
  $("ctl-run").textContent = running ? `Running ${job.fault} on ${job.target}… ${Math.round(job.elapsed_s)} s` : "Run experiment";
  const log = $("ctl-log");
  if (job) {
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
    log.textContent = (c.output || []).join("\n") + (running ? "" : `\n[finished, exit code ${job.exit_code}]`);
    if (!(state.ap && ["running", "reporting"].includes(state.ap.state))) renderFeed(c.output, `${running ? "Running" : "Last run"}: ${plainName(job.fault, job.target, job.params)}`, running, job.target);
    if (atBottom) log.scrollTop = log.scrollHeight;
  }
  if (state.ctl.wasRunning && !running) { $("ctl-approve").checked = false; loadOverview(); }  // new run: refresh charts
  state.ctl.wasRunning = running;
}
async function loadControl() {
  try { renderControl(await api("/api/control/status")); } catch (e) { $("ctl-safety").textContent = "console unavailable: " + e.message; }
}
async function runExperiment() {
  const id = $("ctl-pick").value;
  if (!id || !$("ctl-approve").checked) return;
  $("ctl-run").disabled = true;
  try {
    const r = await fetch("/api/control/run", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ candidate_id: id, approve: true }) });
    if (!r.ok) { const d = await r.json().catch(() => ({})); toast("⛔", "Not started: " + (d.detail || r.status), "bad"); }
    else toast("▶", "Experiment started", "warn");
  } finally { loadControl(); }
}
async function emergencyStop() {
  const b = $("ctl-stop");
  b.disabled = true; b.textContent = "Stopping…";
  const r = await api("/api/control/stop", {}).catch((e) => ({ error: e.message }));
  const msg = r.error ? "Stop failed: " + r.error
    : `Emergency stop: kill switch ${r.kill_switch_trip && r.kill_switch_trip.ok ? "tripped" : "endpoint unavailable"}` +
      (r.rolled_back && r.rolled_back.length ? `, ${r.rolled_back.length} fault(s) rolled back` : ", no active fault to roll back") +
      (r.terminated_run ? ", running experiment stopped." : ".");
  $("ctl-reason").textContent = msg;
  toast(r.error ? "⛔" : "🛑", msg, r.error ? "bad" : "warn");
  b.disabled = false; b.textContent = "Emergency stop";
  loadControl(); loadAutopilot(); loadLive();
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
  .replace(/\$([A-Za-z\\][^$\n]{0,40})\$/g, "$1")  // LaTeX like $p = 0.0$ (starts with a letter), never "$45 and $60"
  .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
  .replace(/`([^`\n]+)`/g, "<code>$1</code>")
  .replace(/^\s*[*-]\s+/gm, "• ");
function openChat(open = true) {
  document.body.classList.toggle("chat-open", open);
  $("chat-fab").setAttribute("aria-expanded", open);
  if (open) { $("chat-fab").classList.remove("unread"); setTimeout(() => $("chat-q").focus(), 50); }
}
function addMsg(role, text, meta) {
  const div = document.createElement("div");
  div.className = "msg " + (role === "user" ? "user" : "bot");
  if (role === "user") div.textContent = text; else div.innerHTML = mdLite(text);
  if (meta) { const s = document.createElement("span"); s.className = "meta"; s.textContent = meta; div.appendChild(s); }
  $("chat-log").appendChild(div); $("chat-log").scrollTop = $("chat-log").scrollHeight;
  if (role !== "user" && !document.body.classList.contains("chat-open") && state.chatReady) $("chat-fab").classList.add("unread");
  return div;
}
async function ask(q) {
  q = q.trim(); if (!q) return;
  openChat(true);
  $("chat-q").value = "";
  addMsg("user", q);
  const pending = addMsg("bot", "Thinking…");
  try {
    const r = await api("/api/chat", { question: q, history: state.history, assumptions: state.assumptions });
    pending.remove();
    addMsg("bot", r.answer, (r.mode === "llm" ? r.model : "offline rules") + (r.note ? " · " + r.note : ""));
    state.history.push({ role: "user", text: r.masked_question }, { role: "assistant", text: r.answer });
    loadUsage();
    state.history = state.history.slice(-8);
  } catch (e) { pending.textContent = "Error: " + e.message; }
}
const CHIPS = ["Summarise the findings report", "What should we test next?", "What is happening right now?", "Did resilience improve between runs?", "What is the business impact?", "Which service is most fragile?", "What should we fix first?", "How is improvement calculated?"];

// Gemini calls used today (the key has a small free quota: answers are cached and calls are capped per day).
async function loadUsage() {
  try {
    const u = (await api("/api/health")).usage; if (!u) return;
    const t = u.paused_until ? `AI paused until ${new Date(u.paused_until * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} (quota)`
      : `AI calls today: ${u.calls}/${u.budget}${u.cache_hits ? ` · ${u.cache_hits} answered from cache` : ""}`;
    document.querySelectorAll(".ai-usage").forEach((el) => { el.textContent = t; el.classList.toggle("worse", !!u.paused_until || u.calls >= u.budget); });
  } catch {}
}

/* ================================================================== WIRING */
function setTab(t) {
  state.tab = t;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
  document.querySelectorAll(".tab-panel").forEach((p) => { p.hidden = p.dataset.panel !== t; });
  try { history.replaceState(null, "", "#" + t); } catch {}
  Object.values(state.charts).forEach((c) => c.resize());
  if (t === "business" && state.data && !state.briefFor) loadBrief();
  if (t === "report" && !state.reportLoaded) { state.reportLoaded = true; loadReportTab(); }
}
function init() {
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
  $("chips").innerHTML = CHIPS.map((c) => `<button type="button">${esc(c)}</button>`).join("");
  $("chips").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));
  $("chat-form").addEventListener("submit", (e) => { e.preventDefault(); ask($("chat-q").value); });
  $("chat-fab").addEventListener("click", () => openChat(!document.body.classList.contains("chat-open")));
  $("chat-close").addEventListener("click", () => openChat(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") openChat(false); });
  $("group").addEventListener("change", (e) => {
    state.groupIdx = +e.target.value;
    const g = state.data.groups[state.groupIdx]; if (g) $("trend-kpi").value = defaultKpi(g);
    renderTrend();
  });
  $("trend-kpi").addEventListener("change", renderTrend);
  $("run-select").addEventListener("change", (e) => { state.runId = e.target.value; renderTimeline(); });
  $("brief-run").addEventListener("change", renderFunnel);
  $("brief-go").addEventListener("click", () => loadBrief(true));
  $("rep-go").addEventListener("click", generateReport);
  $("ctl-run").addEventListener("click", runExperiment);
  $("ctl-stop").addEventListener("click", emergencyStop);
  $("ap-start").addEventListener("click", startAutopilot);
  $("ctl-approve").addEventListener("change", loadControl);
  $("ctl-pick").addEventListener("change", loadControl);
  $("theme").addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("genesis-theme", root.dataset.theme); } catch {}
    if (state.data) { renderTrend(); renderTimeline(); renderBusiness(); }
    if (state.live) renderLive();
  });
  try { const t = localStorage.getItem("genesis-theme"); if (t) document.documentElement.dataset.theme = t; } catch {}
  loadUsage();
  api("/api/health").then((h) => {
    $("chat-model").textContent = h.llm ? h.model : "offline";
    $("chat-mode").textContent = h.llm ? "AI answers grounded in measured runs and live telemetry." : "No GEMINI_API_KEY yet: answering from local rules. Add it to the repo-root .env (see .env.example), no restart needed.";
  }).catch(() => {});
  addMsg("bot", "Hi! Ask me what is happening right now, how a run went, whether we improved, what it costs the business, or what to fix first.");
  state.chatReady = true;
  const fromHash = () => { const t = (location.hash || "").slice(1); return ["live", "experiments", "business", "report"].includes(t) ? t : "live"; };
  window.addEventListener("hashchange", () => { if (fromHash() !== state.tab) setTab(fromHash()); });
  setTab(fromHash());
  loadLive(); loadOverview();
  const hot = () => !!(state.live && (state.live.active_faults.length || state.live.active_toxics.length ||
    Object.values(state.live.services).some((x) => x.failing))) || state.ctl.running || (state.ap && state.ap.state === "running");
  (function liveLoop() { setTimeout(async () => { if (document.visibilityState === "visible") await loadLive(); liveLoop(); }, hot() ? 1500 : 3000); })();
  setInterval(tick, 1000);
  loadControl(); loadAutopilot();
  (function ctlLoop() { setTimeout(() => { if (document.visibilityState === "visible" && (state.tab === "live" || hot())) { loadControl(); loadAutopilot(); } ctlLoop(); }, hot() ? 1500 : 3000); })();
  setInterval(() => { if (document.visibilityState === "visible") loadOverview(); }, 20000);
}
init();
