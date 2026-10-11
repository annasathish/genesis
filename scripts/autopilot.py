"""Autonomous chaos campaign from the command line (same engine as the dashboard's Autopilot button).

    python scripts\autopilot.py            # plan + run 4 experiments, then write the AI findings report
    python scripts\autopilot.py --plan     # only show what it would run

The Planner agent picks the experiments from the dependency map; each one is injected, measured and rolled back
automatically (detection rule, controller kill switch, TTL backstop), one at a time, waiting for the sandbox to be
healthy and the controller's safety gate to allow it. Ctrl+C = emergency stop (kill switch + rollback).
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "controller"))

import insights  # noqa: E402,F401
from agents.coordinator import build_facts, run_pipeline  # noqa: E402
from agents.planner import campaign  # noqa: E402
from insights.dashboard.control import LOG_DIR, Campaign, ExperimentRunner  # noqa: E402
from insights.live import CONTROLLER_URL  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", action="store_true", help="only print the campaign")
    ap.add_argument("--size", type=int, default=4)
    a = ap.parse_args()

    plan = campaign(build_facts()["metrics"], a.size)
    print(f"Autopilot campaign ({len(plan)} experiments, chosen by the Planner agent):")
    for i, c in enumerate(plan, 1):
        print(f"  {i}. {c['fault']} on {c['target']} {c.get('params') or ''}\n     why: {c['reason']}")
    if a.plan:
        return

    runner = ExperimentRunner(CONTROLLER_URL, log_dir=LOG_DIR)
    camp = Campaign(runner, plan_fn=lambda size: campaign(build_facts()["metrics"], size),
                    report_fn=lambda: run_pipeline(use_llm=True), log_dir=LOG_DIR)
    camp.start(a.size)
    printed, last = 0, None
    try:
        while camp.state in ("running", "reporting"):
            st = camp.status()
            cur = next((s for s in st["steps"] if s["status"] in ("waiting", "running")), None)
            if cur and (cur["n"], cur["status"]) != last:
                last = (cur["n"], cur["status"])
                printed = 0 if cur["status"] == "running" else printed
                print(f"\n=== [{cur['n']}/{len(st['steps'])}] {cur['fault']} on {cur['target']}: {cur['status']}"
                      + (f" ({cur.get('waiting_for')})" if cur["status"] == "waiting" and cur.get("waiting_for") else ""))
            out = st["output"]
            if cur and cur["status"] == "running" and len(out) > printed:
                print("\n".join(out[printed:]))
                printed = len(out)
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nEmergency stop: tripping the kill switch and rolling back ...")
        print(camp.stop())
        sys.exit(1)

    st = camp.status()
    print(f"\n==== CAMPAIGN {st['state'].upper()} ====")
    for s in st["steps"]:
        r = s.get("result") or {}
        print(f"  {s['n']}. {s['status']:8s} {s['fault']} on {s['target']}: detect {r.get('time_to_detect', '-')}, "
              f"recover {r.get('time_to_recover', '-')}, peak error {r.get('peak_error_rate', '-')}"
              + (f"\n     proof: {r['evidence']}" if r.get("evidence") else ""))
    if st["report"]:
        print("  findings report:", st["report"])
    sys.exit(0 if st["state"] == "done" else 1)


if __name__ == "__main__":
    main()
