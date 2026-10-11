"""Coordinator: runs Planner -> Analyzer -> Recommender over the measured runs and writes the findings report.

    facts (Python)   runs/*.json -> metrics, run-to-run comparison, business impact, vulnerability board
    Planner          which experiments to run next (dependency map), with a reason each
    Analyzer         findings per experiment, severity ranking, resilience score          (reads facts)
    Recommender      one prioritised fix per weakness, money protected, how to verify it   (reads Analyzer output)
    report           reports/FINDINGS_REPORT.md + reports/findings_report.json (+ agent trace)

Each agent works with or without an LLM (see agents/base.py); the report says which mode each one ran in.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from agents.analyzer import AnalyzerAgent
from agents.planner import PlannerAgent
from agents.recommender import RecommenderAgent
from insights.business import business_summary
from insights.compare import compare_runs
from insights.live import EDGES
from insights.pii import Masker
from insights.runs import DEFAULT_RUNS_DIR, REPO_ROOT, load_runs
from insights.vulnerability import scoreboard

REPORTS_DIR = REPO_ROOT / "reports"


def build_facts(runs_dir: Path = None, assumptions: Dict[str, Any] = None) -> Dict[str, Any]:
    runs = load_runs(runs_dir or DEFAULT_RUNS_DIR, Masker())
    comp = compare_runs(runs)
    return {"metrics": comp["metrics"], "groups": comp["groups"], "method": comp["method"],
            "business": business_summary(comp["metrics"], assumptions),
            "vulnerability": scoreboard(comp["metrics"]), "edges": EDGES, "runs_dir": str(runs_dir or DEFAULT_RUNS_DIR)}


def run_pipeline(runs_dir: Path = None, use_llm: bool = True, key: Optional[str] = None,
                 assumptions: Dict[str, Any] = None, write: bool = True, out_dir: Path = None) -> Dict[str, Any]:
    facts = build_facts(runs_dir, assumptions)
    plan = PlannerAgent(use_llm, key).run(facts)
    analysis = AnalyzerAgent(use_llm, key).run(facts)
    rec = RecommenderAgent(use_llm, key).run({**facts, "analysis": analysis["output"]})
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "experiments_run": len(facts["metrics"]),
        "distinct_experiments": len(analysis["output"]["findings"]),
        "failure_types": analysis["output"]["failure_types"],
        "overall_resilience": analysis["output"]["overall_resilience"],
        "summary": analysis["output"]["summary"],
        "plan": plan["output"],
        "findings": analysis["output"]["findings"],
        "recommendations": rec["output"]["recommendations"],
        "improvement": [{"signature": g["signature"], "runs": g["runs"],
                         "cumulative": g["cumulative"] and {k: g["cumulative"][k] for k in
                                                           ("verdict", "primary_relative_change", "significance")}}
                        for g in facts["groups"]],
        "business_assumptions": facts["business"]["assumptions"],
        "coverage": facts["vulnerability"]["coverage"],
        "agents": [plan["trace"], analysis["trace"], rec["trace"]],
        "sources": {"runs_dir": facts["runs_dir"], "runs": [m["run_id"] for m in facts["metrics"]]},
    }
    report = Masker().mask_obj(report)
    if write:
        out = Path(out_dir or REPORTS_DIR)
        out.mkdir(parents=True, exist_ok=True)
        (out / "findings_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        (out / "FINDINGS_REPORT.md").write_text(to_markdown(report), encoding="utf-8")
        report["files"] = {"markdown": str(out / "FINDINGS_REPORT.md"), "json": str(out / "findings_report.json")}
    return report


def _money(x, cur):
    return "n/a" if x is None else f"{cur} {x:,.0f}"


def _signed(rel):
    """relative_change is positive when error-minutes went down."""
    return "n/a" if rel is None else f"{'-' if rel > 0 else '+'}{abs(rel) * 100:.1f}%"


def _pct(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def to_markdown(r: Dict[str, Any]) -> str:
    cur = r["business_assumptions"].get("currency", "USD")
    L = ["# Findings Report: AI Chaos Engineering on Stan's Robot Shop", "",
         f"Generated {r['generated_at']} by the Genesis multi-agent pipeline (Planner -> Analyzer -> Recommender) "
         f"from {r['experiments_run']} measured run(s) in `runs/`. Every number below is measured or computed by "
         f"Python; the agents explain, rank and recommend.", "",
         "## Executive summary", "",
         (f"- **Overall resilience score: {r['overall_resilience']}/10** (mean of per-experiment scores, `controller/scorer.py`)"
          if r["overall_resilience"] is not None else
          "- **No experiments recorded yet**: run `python scripts/day1_demo.py` (see README), then generate this report again"),
         f"- Experiments: {r['distinct_experiments']} distinct, {r['experiments_run']} runs, failure types: "
         f"{', '.join(r['failure_types'])}",
         f"- Services tested: {r['coverage']['tested']}/{r['coverage']['total']}",
         f"- {r['summary']}", "",
         "## Ranked weaknesses (by severity)", "",
         "| # | Experiment | Severity | Peak error | p95 peak / baseline | Detect | Recover | Resilience |",
         "|---|---|---|---|---|---|---|---|"]
    for f in r["findings"]:
        e = f["evidence"]
        L.append(f"| {f['rank']} | `{f['fault']}` on `{f['target']}` | **{f['severity']}** ({f['severity_score']}) | "
                 f"{_pct(e['peak_error_rate'])} | {e['p95_peak_ms']} / {e['baseline_p95_ms']} ms | "
                 f"{e['time_to_detect_s']} s | {e['time_to_recover_s']} s | {f['resilience_score']}/10 |")
    L.append("")
    for f in r["findings"]:
        L += [f"### {f['rank']}. `{f['fault']}` on `{f['target']}` ({f['severity']})", "", f["explanation"], ""]
        pe = (f.get("evidence") or {}).get("evidence")
        if pe:
            L += [f"Proof the service was down: `GET {pe['path']}` through the shop gateway returned "
                  f"**{pe['status'] or pe['error']}** at +{pe['t']} s (container: {', '.join(f['evidence'].get('services_down') or []) or 'n/a'}).", ""]
        if f.get("run_to_run_verdict"):
            L += [f"Run-to-run ({f['runs']} runs): **{f['run_to_run_verdict']}**.", ""]
    verified = [(f, v) for f in r["findings"] for v in f.get("fix_verification", [])]
    if verified:
        L += ["## Fix verification (re-run after applying a fix)", "",
              "Each fix phase is compared with the runs before it, all runs pooled (two-proportion z-test).", "",
              "| Experiment | Fix | Runs before / after | Error-minutes | Fault error rate | Resilience | p | Verdict |",
              "|---|---|---|---|---|---|---|---|"]
        for f, v in verified:
            p = v["p_value"]
            L.append(f"| `{f['fault']}` on `{f['target']}` | {v['fix']} | {v['runs_before']} / {v['runs_after']} | "
                     f"{v['error_minutes'][0]} -> {v['error_minutes'][1]} min ({_signed(v['error_minutes_change'])}) | "
                     f"{_pct(v['fault_error_rate'][0])} -> {_pct(v['fault_error_rate'][1])} | "
                     f"{v['resilience_score'][0]:.2f} -> {v['resilience_score'][1]:.2f} | {'< 0.000001' if p is not None and p < 1e-6 else p} | "
                     f"**{v['verdict']}** |")
        for f, v in verified:
            if v.get("side_effect"):
                se = v["side_effect"]
                L.append("")
                L.append(f"Side effect found by the re-run: with '{v['fix']}', `{se['endpoint']}` hung at {se['peak_ms']} ms "
                         f"({se['ratio']}x its {se['baseline_ms']} ms baseline) while traffic failed over, which is why the "
                         f"resilience score fell. The errors are gone; the next fix is a short gateway connect timeout.")
        L.append("")
    L += ["## Recommended fixes (prioritised)", "",
          "| Priority | Weakness | Fix | Protects (per year) | Per incident |", "|---|---|---|---|---|"]
    for x in r["recommendations"]:
        p = x["protects"]
        L.append(f"| {x['priority']} | `{x['fault']}` on `{x['service']}` ({x['severity']}) | **{x['fix']}** | "
                 f"{_money(p['annual_exposure'], cur)}{' (est.)' if p['estimated'] else ''} | "
                 f"{_money(p['cost_per_incident'], cur)} |")
    L.append("")
    for x in r["recommendations"]:
        L += [f"**{x['priority']}. {x['fix']} ({x['service']})**: {x['action']}  ", f"Verify: `{x['verify']}`", ""]
    L += ["## Next experiments (Planner)", "", f"Method: {r['plan']['method']}.", "",
          "| Rank | Experiment | Score | Reason |", "|---|---|---|---|"]
    for i, c in enumerate(r["plan"]["ranked"][:8], 1):
        tag = " (selected)" if any(s["id"] == c["id"] for s in r["plan"]["selected"]) else (" (already run)" if c["already_run"] else "")
        L.append(f"| {i} | `{c['id']}`{tag} | {c['score']} | {c.get('ai_reason') or c['reason']} |")
    L += ["", "## Run-to-run improvement", ""]
    for g in r["improvement"]:
        c = g["cumulative"]
        L.append(f"- `{g['signature']}`: {len(g['runs'])} run(s)" + (
            f", first -> latest error-minutes {_signed(c['primary_relative_change'])}, verdict **{c['verdict']}** "
            f"(p = {c['significance']['p_value']})" if c else ", single run"))
    a = r["business_assumptions"]
    L += ["", "## Business assumptions", "",
          f"{a['orders_per_minute']:.0f} orders/min, average order {_money(a['avg_order_value'], cur)}, "
          f"{a['customer_retry_rate'] * 100:.0f}% of customers retry, {a['incidents_per_year']:.0f} incidents/year, "
          f"SLO {a['slo_target'] * 100:.2f}% (`controller/insights/business_config.json`).", "",
          "## How this report was produced (agents)", "", "| Agent | Role | Mode | Model | Time | Notes |", "|---|---|---|---|---|---|"]
    for t in r["agents"]:
        L.append(f"| {t['agent']} | {t['role']} | {t['mode']} | {t['model'] or '-'} | {t['duration_s']} s | "
                 f"{'; '.join(t['notes']) or '-'} |")
    L += ["", f"Source runs: {', '.join(r['sources']['runs'])}", ""]
    return "\n".join(L)
