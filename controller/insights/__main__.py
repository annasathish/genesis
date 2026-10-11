"""Terminal summary of run-to-run improvement and business impact (same numbers as the dashboard).

    python -m insights                 # from controller/   (make insights)
    python -m insights --json          # machine-readable
"""
import argparse
import json

from insights.business import business_summary
from insights.compare import compare_runs
from insights.pii import Masker
from insights.runs import DEFAULT_RUNS_DIR, load_runs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", default=str(DEFAULT_RUNS_DIR))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    masker = Masker()
    comp = compare_runs(load_runs(a.runs, masker))
    biz = business_summary(comp["metrics"])
    if a.json:
        print(json.dumps({"comparison": comp, "business": biz, "pii": masker.report()}, indent=2, default=str))
        return

    cur = biz["assumptions"]["currency"]
    print(f"{len(comp['metrics'])} runs in {a.runs}  (PII items masked: {masker.report()['total_masked']})\n")
    for i, (m, b) in enumerate(zip(comp["metrics"], biz["runs"]), 1):
        print(f"Run {i} {m['run_id']} {m['fault']}->{m['target']} {m['label']}")
        print(f"   score {m['resilience_score']}/10  peak err {m['peak_error_rate']:.1%}  error-min {m['error_minutes']}"
              f"  detect {m['time_to_detect_s']}s  recover {m['time_to_recover_s']}s")
        print(f"   cost/incident {cur} {b['cost_per_incident']:,.0f}  orders lost {b['orders_lost']}"
              f"  per year {cur} {b['annual_exposure']:,.0f}{'  (estimated)' if b['estimated'] else ''}")
    for g in comp["groups"]:
        if not g["steps"]:
            continue
        print(f"\n{g['signature']}: improvement (primary KPI error-minutes, z-test p<0.05)")
        for s in g["steps"] + [g["cumulative"]]:
            rel = s["primary_relative_change"]
            print(f"   {s['from']} -> {s['to']}: {'' if rel is None else f'{-rel:+.1%}'}"
                  f"  p={s['significance']['p_value']}  => {s['verdict']}")


if __name__ == "__main__":
    main()
