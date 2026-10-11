"""Multi-agent findings report from the command line (same pipeline as the dashboard's "Generate report" button).

    python -m agents report            # from controller/  (make findings)  -> reports/FINDINGS_REPORT.md + .json
    python -m agents report --no-llm   # rules only, no API calls
    python -m agents plan              # just the Planner's ranked experiments
"""
import argparse

from agents.coordinator import build_facts, run_pipeline
from agents.planner import PlannerAgent


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["report", "plan"])
    ap.add_argument("--no-llm", action="store_true", help="rule-based agents only")
    a = ap.parse_args()
    if a.command == "plan":
        out = PlannerAgent(use_llm=not a.no_llm).run(build_facts())
        for i, c in enumerate(out["output"]["ranked"][:10], 1):
            sel = "*" if any(s["id"] == c["id"] for s in out["output"]["selected"]) else " "
            print(f"{sel} {i:2d}. {c['score']:5.1f}  {c['id']:38s} {c.get('ai_reason') or c['reason']}")
        print(f"\n* = selected (budget {out['output']['budget']} experiments, {out['output']['already_run']} already run)"
              f" | planner mode: {out['trace']['mode']} {out['trace']['notes'] or ''}")
        return
    r = run_pipeline(use_llm=not a.no_llm)
    if r["overall_resilience"] is None:
        print("No experiments recorded yet (runs/ is empty): run python scripts/day1_demo.py first.")
    else:
        print(f"Overall resilience {r['overall_resilience']}/10 over {r['distinct_experiments']} experiment(s), "
              f"{len(r['failure_types'])} failure type(s)")
    for f in r["findings"]:
        print(f"  {f['rank']}. {f['severity']:8s} {f['severity_score']:5.1f}  {f['fault']} on {f['target']}")
    for t in r["agents"]:
        print(f"  agent {t['agent']:11s} {t['mode']:5s} {t['model'] or '':18s} {t['duration_s']:5.1f}s {'; '.join(t['notes'])}")
    print(f"Written: {r['files']['markdown']}\n         {r['files']['json']}")


if __name__ == "__main__":
    main()
