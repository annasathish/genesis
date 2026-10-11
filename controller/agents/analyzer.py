"""Analyzer agent: explains what each experiment showed, ranks weaknesses by severity, scores resilience.

One finding per experiment (signature = fault + target + params), from its latest run; earlier runs of the
same experiment feed the run-to-run verdict (insights/compare.py).

  severity score (0-100) = 100 x (0.4 x error spike + 0.3 x min(1, incident s / 180) + 0.3 x impacted
                           services / 7)   -- the H_v formula of the vulnerability board, per experiment
                           + up to 20 for slowdown: min(20, 20 x (p95 ratio - 1) / 9) when p95 rose
  error spike            worst of the overall peak error rate and the peak checkout failure rate (share of
                         expected checkouts that did not complete in a window): a queue outage that fails every
                         checkout but only ~6 % of all requests is still severe for the business
  severity               Critical >= 60, High >= 40, Medium >= 20, Low otherwise
  resilience score       controller/scorer.py (M5), 0-10 per experiment; overall = mean over experiments
Numbers come from Python. The LLM may only rewrite the explanation and re-order findings of equal evidence; it
cannot change a severity, a score or a number.
"""
from statistics import mean
from typing import Any, Dict, List

from agents.base import Agent, accept_text
from insights.compare import BASELINE_PHASE, REVERTED_PHASE, phase_of
from insights.severity import impacted_services, level, severity  # noqa: F401  (severity re-exported)

# severity formula lives in insights/severity.py so the vulnerability board uses exactly the same one
_level, _impacted = level, impacted_services


def _explain(m: Dict[str, Any], sev: Dict[str, Any]) -> str:
    target = (f"the {m['edge']['caller']} -> {m['edge']['dependency']} call" if m.get("edge") else m["target"])
    lines = [f"The {m['fault']} fault on {target} pushed the error rate to {m['peak_error_rate'] * 100:.1f}% at peak"
             f" ({m['error_minutes']} error-minutes)" if m["peak_error_rate"] > 0 else
             f"The {m['fault']} fault on {target} caused no request errors"]
    se = m.get("slowest_endpoint") or {}
    if (se.get("ratio") or 0) > 1.5:
        lines.append(f"{se['endpoint']} slowed to {se['peak_ms']} ms mean ({se['ratio']}x its {se['baseline_ms']} ms baseline)")
    elif sev["p95_ratio"] > 1.5:
        lines.append(f"p95 latency rose to {m['p95_peak_ms']} ms ({sev['p95_ratio']}x the {m['baseline_p95_ms']} ms baseline)")
    if (m.get("checkout_peak_loss") or 0) >= 0.5:
        lines.append(f"at its worst {m['checkout_peak_loss'] * 100:.0f}% of expected checkouts did not complete")
    imp = [s for s in _impacted(m) if s != m.get("service")]
    if imp:
        lines.append(f"the failure cascaded to {', '.join(imp)} (failing endpoints: {', '.join(m['endpoints_failing'])})")
    elif m.get("endpoints_failing"):
        lines.append(f"failing endpoints: {', '.join(m['endpoints_failing'])}")
    else:
        lines.append("no endpoint failed: the system absorbed the fault")
    ev = m.get("evidence")
    if ev:
        lines.append(f"proof: GET {ev['path']} through the shop returned {ev['status'] or ev['error']} at +{ev['t']} s")
    if m.get("time_to_detect_s") is not None:
        lines.append(f"detected after {m['time_to_detect_s']} s")
    else:
        lines.append("the kill-switch rule never tripped")
    lines.append(f"recovered {m['time_to_recover_s']} s after rollback" if m.get("recovered")
                 else "did not recover within the wait window")
    s = "; ".join(lines) + "."
    return s[0].upper() + s[1:]


def readable(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Evidence with units, as the LLM should quote it (ratios as %, times in s, latency in ms)."""
    pct = lambda x: None if x is None else f"{x * 100:.1f}%"
    se = ev.get("slowest_endpoint") or {}
    return {
        "peak_error_rate": pct(ev.get("peak_error_rate")),
        "error_rate_during_fault": pct(ev.get("fault_error_rate")),
        "worst_checkout_failure": pct(ev.get("checkout_peak_loss")),
        "checkouts_lost_in_sandbox": ev.get("sandbox_checkouts_lost"),
        "failed_requests": ev.get("failed_requests"),
        "p95": f"{ev.get('p95_peak_ms')} ms (baseline {ev.get('baseline_p95_ms')} ms)",
        "slowest_endpoint": f"{se['endpoint']} {se['peak_ms']} ms mean vs {se['baseline_ms']} ms baseline ({se['ratio']}x)" if se else None,
        "time_to_detect": None if ev.get("time_to_detect_s") is None else f"{ev['time_to_detect_s']} s",
        "time_to_recover_after_rollback": None if ev.get("time_to_recover_s") is None else f"{ev['time_to_recover_s']} s",
        "failing_endpoints": ev.get("endpoints_failing"), "slow_endpoints": ev.get("endpoints_slow"),
        "containers_down": ev.get("services_down"), "recovered": ev.get("recovered"),
        "health_probe_failure": (f"GET {ev['evidence']['path']} via the shop -> {ev['evidence']['status'] or ev['evidence']['error']}"
                                 f" at +{ev['evidence']['t']} s") if ev.get("evidence") else None,
    }


class AnalyzerAgent(Agent):
    name = "analyzer"
    role = "Explains each experiment's findings, ranks weaknesses by severity, gives a resilience score"
    system = """You are the Analyzer agent of a chaos-engineering platform for an e-commerce app.
You receive measured findings (one per experiment) with severity and scores already computed by Python.
Return JSON: {"findings": [{"id": <finding id>, "explanation": <2-3 sentences: what failed, why (likely root
cause given the dependency path), and what users experienced>}], "summary": <2 sentences on the overall
resilience picture>}.
Rules: use only ids from the input; quote numbers exactly as written in the evidence (with their units, e.g.
"18.2%", "3141.9 ms"), never as raw fractions; never invent numbers, services or
causes not supported by the input; do not change severities or scores."""

    def rules(self, facts):
        groups = {}
        for m in facts["metrics"]:
            groups.setdefault(m["signature"], []).append(m)
        verdicts = {g["signature"]: (g["cumulative"] or {}).get("verdict") for g in facts.get("groups", [])}
        effects = {g["signature"]: g.get("effects", []) for g in facts.get("groups", [])}
        findings = []
        for sig, ms in groups.items():
            # the weakness describes the system as it is: the latest run with no fix applied
            base = [x for x in ms if phase_of(x["label"]) == BASELINE_PHASE]
            m = (base or ms)[-1]
            sev = severity(m)
            fixes = []
            for e in effects.get(sig, []):
                if e["to_phase"] == BASELINE_PHASE or e["to_phase"] == REVERTED_PHASE:
                    continue   # only phases that applied a fix are fix verifications
                after = [x for x in ms if phase_of(x["label"]) == e["to_phase"]]
                se = (after[-1].get("slowest_endpoint") if after else None) or {}
                fixes.append({"fix": e["to_phase"], "verdict": e["verdict"], "runs_before": e["from_runs"],
                              "runs_after": e["to_runs"],
                              "error_minutes": [e["deltas"]["error_minutes"]["old"], e["deltas"]["error_minutes"]["new"]],
                              "error_minutes_change": e["primary_relative_change"],
                              "fault_error_rate": [e["deltas"]["fault_error_rate"]["old"], e["deltas"]["fault_error_rate"]["new"]],
                              "resilience_score": [e["deltas"]["resilience_score"]["old"], e["deltas"]["resilience_score"]["new"]],
                              "p_value": e["significance"]["p_value"],
                              "side_effect": se if (se.get("ratio") or 0) >= 5 else None})
            findings.append({
                "id": sig, "fault": m["fault"], "target": m["target"], "service": m.get("service", m["target"]),
                "edge": m.get("edge"), "params": m.get("params") or {}, "runs": len(ms), "latest_run": m["run_id"],
                "resilience_score": m["resilience_score"], **sev,
                "evidence": {k: m.get(k) for k in ("peak_error_rate", "fault_error_rate", "error_minutes",
                                                   "failed_requests", "baseline_p95_ms", "p95_peak_ms",
                                                   "time_to_detect_s", "time_to_recover_s", "incident_s",
                                                   "checkout_peak_loss", "sandbox_checkouts_lost", "evidence",
                                                   "endpoints_failing", "endpoints_slow", "slowest_endpoint",
                                                   "services_down", "recovered")},
                "run_to_run_verdict": verdicts.get(sig) if len(base or ms) > 1 and not fixes else None,
                "fix_verification": fixes,
                "explanation": _explain(m, sev),
            })
        findings.sort(key=lambda f: (-f["severity_score"], f["resilience_score"]))
        for i, f in enumerate(findings, 1):
            f["rank"] = i
        scores = [f["resilience_score"] for f in findings]
        overall = round(mean(scores), 2) if scores else None
        worst = findings[0] if findings else None
        summary = (f"{len(findings)} experiment(s) across {len({f['fault'] for f in findings})} failure type(s); "
                   f"overall resilience {overall}/10. Most severe: {worst['fault']} on {worst['target']} "
                   f"({worst['severity']}, {worst['severity_score']}/100).") if worst else "No experiments yet."
        return {"findings": findings, "overall_resilience": overall, "summary": summary,
                "failure_types": sorted({f["fault"] for f in findings})}

    def llm_task(self, facts, draft):
        if not draft["findings"]:
            return None
        return {"findings": [{**{k: f[k] for k in ("id", "fault", "target", "service", "edge", "severity",
                                                    "severity_score", "resilience_score", "runs", "run_to_run_verdict")},
                              "evidence": readable(f["evidence"])} for f in draft["findings"]],
                "overall_resilience": draft["overall_resilience"],
                "dependency_edges": facts.get("edges", [])}

    def merge(self, draft, answer, allowed, notes):
        if not isinstance(answer, dict):
            return draft
        texts = {str(f.get("id")): f.get("explanation") for f in answer.get("findings", []) if isinstance(f, dict)}
        out = []
        for f in draft["findings"]:
            f = dict(f)
            txt = accept_text(f"explanation[{f['id']}]", texts.get(f["id"]), allowed, notes)
            if txt:
                f["rule_explanation"], f["explanation"], f["explained_by"] = f["explanation"], txt, "ai"
            out.append(f)
        summary = accept_text("summary", answer.get("summary"), allowed, notes) or draft["summary"]
        return {**draft, "findings": out, "summary": summary}
