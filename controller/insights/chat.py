"""Resilience Copilot: a chat assistant grounded in the platform's own data.

Grounding rules (same spirit as the M5 analyzer):
  - Python computes every number; the assistant only explains them. The LLM gets a fact sheet and is told
    to answer only from it and to say so when the answer is not there.
  - PII: the fact sheet, the chat history and the user's question are masked before any LLM call, and the
    answer is masked again before it is returned (a pasted email in a question never leaves the machine).
  - Real model: Gemini (insights/llm.py) whenever GEMINI_API_KEY is set. Without a key, or if the call
    fails, a deterministic intent engine answers from the same facts and the UI shows why.
"""
import json
import re
from typing import Any, Dict, List, Optional

from insights import llm
from insights.pii import APPLIED_AT, POLICY, Masker

MAX_HISTORY = 8
MAX_QUESTION = 1000

# Fix vocabulary shared with controller/analyzer.py (kept in sync by hand: importing analyzer pulls in the
# Gemini SDK, which the host dashboard does not need).
FIX_BY_FAULT = {
    "container_crash": "Increase Replicas",
    "network_latency": "Adjust Timeouts",
    "disk_fill": "Add Retries",
    "disk_stress": "Add Retries",
    "rabbitmq_block": "Implement Circuit Breaker",
}

SYSTEM_PROMPT = """You are Resilience Copilot for a chaos-engineering platform that injects faults into a \
sandboxed e-commerce app (Stan's Robot Shop) and measures the impact.
Rules:
- Answer ONLY from the FACTS JSON below. Never invent numbers, runs, services or causes. If the facts do not \
contain the answer, say what is missing and which experiment would produce it.
- Quote numbers exactly as they appear in FACTS, with units. Cite run ids when you use a run's numbers.
- An improvement between runs may only be called real if its verdict in FACTS is "improved"; explain \
"no significant change" and "mixed" verdicts honestly.
- Business figures are projections from the stated assumptions; say so when you use them.
- LIVE holds the current system state (status, live error rate, failing services, active faults); use it for \
"what is happening now" questions and say when telemetry is unavailable.
- findings_report (when present) is the latest multi-agent report (Analyzer findings ranked by severity, \
Recommender fixes by priority); next_experiments are the Planner agent's selected experiments with reasons.
- Never output personal data. Tokens like user_ab12cd or [REDACTED] are masked on purpose: do not try to \
reconstruct them.
- Be concise: at most ~150 words unless asked for detail. Plain text, short bullet points allowed.
"""


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _change(rel: Optional[float]) -> str:
    """Relative change of a lower-is-better KPI, in words."""
    if rel is None:
        return "n/a"
    return f"{abs(rel) * 100:.1f}% {'reduction' if rel >= 0 else 'increase'}"


def _money(x: float, cur: str) -> str:
    return f"{cur} {x:,.0f}"


class Copilot:
    def __init__(self, facts: Dict[str, Any], masker: Masker = None, api_key: str = None):
        self.masker = masker or Masker()
        self.facts = facts
        self.api_key = api_key if api_key is not None else llm.api_key()

    # ------------------------------------------------------------------ public
    def ask(self, question: str, history: List[Dict[str, str]] = None) -> Dict[str, Any]:
        question = self.masker.mask_text((question or "").strip()[:MAX_QUESTION])
        history = [{"role": h.get("role", "user"), "text": self.masker.mask_text(str(h.get("text", ""))[:MAX_QUESTION])}
                   for h in (history or [])[-MAX_HISTORY:]]
        if not question:
            return {"answer": "Ask me about run-to-run improvement, business impact, recovery times or PII "
                              "masking.", "mode": "offline", "masked_question": question}
        if self.api_key:
            try:
                out = self._llm(question, history)
                return {"answer": self.masker.mask_text(out["text"]), "mode": "llm", "model": out["model"],
                        "masked_question": question}
            except llm.LLMError as exc:
                return {"answer": self._offline(question), "mode": "offline", "masked_question": question,
                        "note": f"{exc}; answered from local rules."}
        return {"answer": self._offline(question), "mode": "offline", "masked_question": question}

    # ------------------------------------------------------------------ LLM path
    def _llm(self, question: str, history: List[Dict[str, str]]) -> Dict[str, str]:
        facts = json.dumps(self.masker.mask_obj(self.facts), default=str, separators=(",", ":"))
        contents = [{"role": "model" if h["role"] == "assistant" else "user", "parts": [{"text": h["text"]}]}
                    for h in history if h["text"]]
        contents.append({"role": "user", "parts": [{"text": question}]})
        return llm.generate(SYSTEM_PROMPT + "\nFACTS:\n" + facts, contents, key=self.api_key)

    # ------------------------------------------------------------------ offline path
    def _offline(self, q: str) -> str:
        ql = q.lower()
        run = self._mentioned_run(ql)
        if run:
            return self._a_run(run)
        intents = [
            (r"\b(now|right now|currently|live|status|happening|at the moment)\b", self._a_live),
            (r"next experiment|test next|what to test|\bplan\b|planner", self._a_plan),
            (r"\breport\b|findings|severity|weakness", self._a_report),
            (r"how .*(calculat|comput|measur|work out)|method|formula|definition", self._a_method),
            (r"pii|privacy|personal|mask|gdpr|dpdp|sensitive|anonymi|pseudonym", self._a_pii),
            (r"business|revenue|cost|money|loss|lost|order|customer|impact|dollar|\$|rupee|slo|budget", self._a_business),
            (r"improv|compar|trend|better|worse|progress|regress|change|between runs|50", self._a_compare),
            (r"recommend|fix|should|next|suggest|action", self._a_recommend),
            (r"recover|mttr|detect|ttd|ttr|how long|downtime", self._a_times),
            (r"weak|worst|critical|risk|fragile|score", self._a_worst),
            (r"^(hi|hello|hey|help)\b|what can you", self._a_help),
        ]
        for pattern, fn in intents:
            if re.search(pattern, ql):
                return fn()
        return self._a_overview()

    def _metrics(self) -> List[Dict[str, Any]]:
        return self.facts.get("metrics", [])

    def _mentioned_run(self, ql: str) -> Optional[Dict[str, Any]]:
        ms = self._metrics()
        for m in ms:
            if m["run_id"].lower() in ql:
                return m
        hit = re.search(r"\brun\s*#?\s*(\d+)\b", ql)
        if hit and 1 <= int(hit.group(1)) <= len(ms):
            return ms[int(hit.group(1)) - 1]
        if re.search(r"\b(latest|last|most recent) run\b", ql) and ms:
            return ms[-1]
        return None

    def _biz_for(self, run_id: str) -> Optional[Dict[str, Any]]:
        return next((b for b in self.facts.get("business", {}).get("runs", []) if b["run_id"] == run_id), None)

    def _a_live(self) -> str:
        live = self.facts.get("live")
        if not live:
            return "Live telemetry is not available right now."
        w = live.get("window") or {}
        failing = [s for s, v in live.get("services", {}).items() if v.get("failing")]
        ks = live.get("kill_switch", {})
        lines = [f"Now: {live['status']['text']}."]
        if w:
            lines.append(f"- Last window: error rate {_pct(w.get('err'))} over {w.get('req')} requests, "
                         f"{w.get('rps')} req/s, p95 {w.get('p95_ms')} ms.")
        else:
            lines.append("- No live traffic window yet (Locust unreachable or first sample).")
        lines.append(f"- Failing services: {', '.join(failing) or 'none'}.")
        lines.append(f"- Kill-switch rule: {'TRIPPED' if ks.get('tripped') else 'armed'} "
                     f"({ks.get('breach_windows', 0)}/{ks.get('consecutive_breach_windows', 2)} breach windows).")
        if live.get("active_faults"):
            lines.append("- Active faults: " + ", ".join(f"{a['fault']} on {a['target']}" for a in live["active_faults"]))
        return "\n".join(lines)

    def _a_plan(self) -> str:
        nxt = self.facts.get("next_experiments")
        if not nxt:
            return "No plan yet: generate the AI findings report (AI report tab) to get the Planner's next experiments."
        return "Planner agent, next experiments:\n" + "\n".join(
            f"- {c['id']} (score {c['score']}): {c['reason']}" for c in nxt)

    def _a_report(self) -> str:
        r = self.facts.get("findings_report")
        if not r:
            return "No findings report yet: press Generate on the AI report tab."
        if r.get("overall_resilience") is None:
            return "The findings report has no experiments yet: run an experiment, then press Generate again."
        lines = [f"Findings report ({r['generated_at']}): overall resilience {r['overall_resilience']}/10."]
        lines += [f"- #{f['rank']} {f['severity']} ({f['severity_score']}): {f['fault']} on {f['target']}"
                  for f in (r.get("findings") or [])[:5]]
        recs = r.get("recommendations") or []
        if recs:
            lines.append(f"Top fix: {recs[0]['fix']} for {recs[0]['service']}: {recs[0]['action']}")
        return "\n".join(lines)

    def _a_help(self) -> str:
        return ("I answer from this platform's measured runs and live telemetry. Try:\n"
                "- What is happening right now?\n"
                "- Did resilience improve between runs, and is it significant?\n"
                "- What is the business impact of the catalogue crash?\n"
                "- How is improvement calculated?\n"
                "- How is personal data protected?\n"
                "- Tell me about run 2 / the latest run\n"
                "- What should we fix first?")

    def _a_overview(self) -> str:
        ms = self._metrics()
        if not ms:
            return "No runs recorded yet. Run `python scripts/day1_demo.py` to produce one."
        latest = ms[-1]
        return (f"{len(ms)} runs recorded across {len(self.facts.get('groups', []))} experiment type(s). "
                f"Latest ({latest['run_id']}, {latest['fault']} on {latest['target']}): score "
                f"{latest['resilience_score']}/10, peak error {_pct(latest['peak_error_rate'])}, "
                f"{latest['error_minutes']} error-minutes, recovered in {latest['time_to_recover_s']} s.\n"
                + self._a_help())

    def _a_run(self, m: Dict[str, Any]) -> str:
        b = self._biz_for(m["run_id"])
        cur = self.facts.get("business", {}).get("assumptions", {}).get("currency", "USD")
        lines = [
            f"Run {m['run_id']}{' (' + m['label'] + ')' if m['label'] else ''}: {m['fault']} on {m['target']}.",
            f"- Peak error {_pct(m['peak_error_rate'])}, fault-phase error {_pct(m['fault_error_rate'])} "
            f"over {m['fault_requests']} requests; {m['error_minutes']} error-minutes.",
            f"- Detected in {m['time_to_detect_s']} s, recovered {m['time_to_recover_s']} s after rollback.",
            f"- Blast radius: {m['blast_radius_endpoints']} endpoints ({', '.join(m['endpoints_failing']) or 'none'}).",
            f"- Resilience score {m['resilience_score']}/10.",
        ]
        if b:
            lines.append(f"- Projected cost per incident {_money(b['cost_per_incident'], cur)} "
                         f"({b['orders_lost']} orders lost at production traffic"
                         f"{', estimated' if b['estimated'] else ''}).")
        return "\n".join(lines)

    def _a_compare(self) -> str:
        groups = [g for g in self.facts.get("groups", []) if g["steps"]]
        if not groups:
            return ("Improvement needs at least two runs of the same experiment (same fault, target and "
                    "parameters). Re-run the same experiment after a fix, labelled with --label.")
        out = []
        for g in groups:
            out.append(f"{g['fault']} on {g['target']} ({len(g['runs'])} runs), primary KPI = error-minutes:")
            for s in g["steps"]:
                d = s["deltas"]["error_minutes"]
                out.append(f"- {s['from']} -> {s['to']}: {d['old']} -> {d['new']} min "
                           f"({_change(s['primary_relative_change'])}), p = {s['significance']['p_value']}"
                           f" -> {s['verdict'].upper()}")
            c = g["cumulative"]
            out.append(f"- First -> latest: {_change(c['primary_relative_change'])} in error-minutes, but fault-phase "
                       f"error rate {_pct(c['deltas']['fault_error_rate']['old'])} -> "
                       f"{_pct(c['deltas']['fault_error_rate']['new'])}: verdict {c['verdict'].upper()}.")
        out.append("A change only counts as improved if it is statistically significant (p < 0.05) and at "
                   "least 10 % in the same direction as the error rate; otherwise it is reported as noise or mixed.")
        return "\n".join(out)

    def _a_method(self) -> str:
        meth = self.facts.get("method", {})
        return ("How improvement is calculated:\n"
                f"- Comparable runs: {meth.get('comparability', '')}\n"
                f"- Relative change: {meth.get('relative_change', '')} Example: 50 -> 30 -> 20 -> 10 is "
                "40 %, 33 %, 50 % per step, 80 % cumulative.\n"
                f"- Primary KPI: {meth.get('primary_kpi', '')}\n"
                f"- Significance: {meth.get('significance', '')}\n"
                f"- {meth.get('resolution', '')}\n"
                f"- {meth.get('attribution', '')}")

    def _a_business(self) -> str:
        biz = self.facts.get("business", {})
        rows = biz.get("runs", [])
        if not rows:
            return "No runs to price yet."
        a = biz["assumptions"]
        cur = a["currency"]
        worst = biz["worst_run"]
        out = [f"Projected at production traffic ({a['orders_per_minute']:.0f} orders/min, AOV "
               f"{_money(a['avg_order_value'], cur)}, {a['customer_retry_rate']*100:.0f} % of customers retry):",
               f"- Worst run {worst['run_id']}: {worst['orders_lost']} orders lost, revenue at risk "
               f"{_money(worst['revenue_at_risk'], cur)}, cost per incident {_money(worst['cost_per_incident'], cur)}, "
               f"{_money(worst['annual_exposure'], cur)}/yr at {a['incidents_per_year']:.0f} incidents/yr, "
               f"{worst['slo_budget_burn_pct']} % of the monthly error budget per incident."]
        verdicts = {g["signature"]: g["cumulative"]["verdict"] for g in self.facts.get("groups", []) if g["cumulative"]}
        for t in biz.get("trends", []):
            if t["cumulative"]:
                c = t["cumulative"]
                v = verdicts.get(t["signature"], "no significant change")
                line = (f"- {t['signature']}: first -> latest run, cost per incident moved by "
                        f"{_money(-c['saved_per_incident'], cur)} ({_money(c['saved_per_year'], cur)}/yr).")
                if v == "improved":
                    line += " The improvement is statistically significant, so this is a real saving."
                else:
                    line += f" Verdict is {v.upper()}, so this is not claimed as a saving."
                out.append(line)
        if biz.get("any_estimated"):
            out.append("Note: older runs lack per-endpoint data, so their checkout loss is estimated from the "
                       "aggregate error rate. New runs measure it directly.")
        return "\n".join(out)

    def _a_times(self) -> str:
        ms = self._metrics()
        if not ms:
            return "No runs yet."
        rows = [f"- {m['run_id']}: detect {m['time_to_detect_s']} s, recover {m['time_to_recover_s']} s, "
                f"incident {m['incident_s']} s" for m in ms]
        return "Detection and recovery (measured in 5 s windows):\n" + "\n".join(rows)

    def _a_worst(self) -> str:
        ms = self._metrics()
        if not ms:
            return "No runs yet."
        w = min(ms, key=lambda m: m["resilience_score"])
        return (f"Weakest result: {w['fault']} on {w['target']} in run {w['run_id']} with score "
                f"{w['resilience_score']}/10 (peak error {_pct(w['peak_error_rate'])}, "
                f"{w['blast_radius_endpoints']} endpoints affected: {', '.join(w['endpoints_failing'])}).")

    def _a_recommend(self) -> str:
        ms = self._metrics()
        if not ms:
            return "No runs yet: run an experiment first."
        w = min(ms, key=lambda m: m["resilience_score"])
        fix = FIX_BY_FAULT.get(w["fault"], "Add Retries")
        tips = [f"1. {fix} for {w['target']} (weakest: {w['resilience_score']}/10 under {w['fault']})."]
        if any("cart:add" in m["endpoints_failing"] for m in ms) and w["target"] == "catalogue":
            tips.append("2. cart depends synchronously on catalogue (cart:add fails when catalogue is down): "
                        "cache product data in cart or add a circuit breaker so checkout degrades gracefully.")
        if any((m["time_to_detect_s"] or 0) > 30 for m in ms):
            tips.append("3. Detection took over 30 s in some runs: alert on per-endpoint errors, not only the "
                        "aggregate error rate.")
        tips.append("Then re-run the same experiment with --label describing the fix, so the dashboard can "
                    "show whether it was a significant improvement.")
        return "Recommended next steps:\n" + "\n".join(tips)

    def _a_pii(self) -> str:
        stats = self.facts.get("pii", {})
        lines = ["How personal data is protected:"]
        lines += [f"- {p['data']}: {p['treatment']}" for p in POLICY]
        lines.append("Applied at: " + "; ".join(APPLIED_AT) + ".")
        lines.append(f"Items masked in the data currently loaded: {stats.get('total_masked', 0)}.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------------------------- per-run AI brief
BRIEF_PROMPT = """You are an SRE writing a short incident brief for engineers and business stakeholders about one \
chaos experiment on an e-commerce app. Use ONLY the numbers in DATA, copied exactly with units; never invent \
numbers. Business figures are projections from the listed assumptions: say "projected". Write 4 short parts, \
each starting with its label on a new line:
What happened: 1-2 sentences.
What it affects: users, journey stages and dependent services.
Time and money: detection/recovery time, orders lost, revenue at risk, cost per incident, error budget used.
Recommendation: one concrete architectural fix for this failure, and how to verify it (re-run the experiment).
Under 140 words total. Plain text."""
_BRIEF_CACHE: Dict[str, Dict[str, Any]] = {}


def _brief_data(m: Dict[str, Any], b: Dict[str, Any], a: Dict[str, Any]) -> Dict[str, Any]:
    keep_m = ("fault", "target", "label", "peak_error_rate", "fault_error_rate", "error_minutes", "time_to_detect_s",
              "time_to_recover_s", "incident_s", "endpoints_failing", "services_down", "resilience_score")
    keep_b = ("orders_lost", "revenue_at_risk", "revenue_lost", "ops_cost", "support_tickets", "cost_per_incident",
              "annual_exposure", "slo_budget_burn_pct", "churn_risk", "estimated")
    return {"run": {k: m.get(k) for k in keep_m}, "business": {k: b.get(k) for k in keep_b},
            "blocked_stages": [f["stage"] for f in b.get("funnel", []) if f["blocked"]],
            "assumptions": {k: a.get(k) for k in ("currency", "orders_per_minute", "avg_order_value",
                                                  "incidents_per_year", "slo_target")}}


def offline_brief(m: Dict[str, Any], b: Dict[str, Any], a: Dict[str, Any]) -> str:
    cur = a.get("currency", "USD")
    stages = [f["stage"] for f in b.get("funnel", []) if f["blocked"]]
    fix = FIX_BY_FAULT.get(m["fault"], "Add Retries")
    return (f"What happened: {m['fault']} on {m['target']} pushed the error rate to a peak of "
            f"{_pct(m['peak_error_rate'])} ({m['error_minutes']} error-minutes).\n"
            f"What it affects: {', '.join(stages) or 'no shopper journey stage'}; failing endpoints: "
            f"{', '.join(m['endpoints_failing']) or 'none'}. Churn risk {b['churn_risk']}.\n"
            f"Time and money: detected in {m['time_to_detect_s']} s, recovered {m['time_to_recover_s']} s after "
            f"rollback. Projected {b['orders_lost']} orders lost, {_money(b['revenue_at_risk'], cur)} revenue at risk, "
            f"{_money(b['cost_per_incident'], cur)} per incident, {b['slo_budget_burn_pct']} % of the monthly error "
            f"budget.\n"
            f"Recommendation: {fix} for {m['target']}, then re-run the same experiment with --label to confirm a "
            f"significant improvement.")


def run_brief(m: Dict[str, Any], b: Dict[str, Any], a: Dict[str, Any], key: Optional[str] = None,
              masker: Masker = None) -> Dict[str, Any]:
    masker = masker or Masker()
    data = masker.mask_obj(_brief_data(m, b, a))
    cache_key = json.dumps(data, sort_keys=True, default=str)
    if cache_key in _BRIEF_CACHE:
        return _BRIEF_CACHE[cache_key]
    key = key if key is not None else llm.api_key()
    try:
        out = llm.generate(BRIEF_PROMPT, [{"role": "user", "parts": [{"text": "DATA:\n" + json.dumps(data, default=str)}]}],
                           key=key, max_tokens=400)
        res = {"brief": masker.mask_text(out["text"]), "mode": "llm", "model": out["model"]}
        _BRIEF_CACHE[cache_key] = res  # cache only real answers, so a newly added key is used on the next request
    except llm.LLMError as exc:
        res = {"brief": offline_brief(m, b, a), "mode": "offline", "note": str(exc)}
    return res
