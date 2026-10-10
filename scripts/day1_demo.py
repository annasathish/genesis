"""Day-1 end-to-end chaos run: baseline -> inject -> detect -> rollback -> verify recovery.

Stdlib only, runs on the host (Windows cmd or bash):
    python scripts/day1_demo.py                          # crash `catalogue`
    python scripts/day1_demo.py --target redis           # any service with com.chaospilot.role=target

What it proves, with numbers:
  1. steady state  : 30 s of Locust traffic, error rate + p95 (baseline)
  2. injection     : POST /api/v1/inject on the controller (M3 ChaosManager, intent logged in SQLite,
                     TTL auto-rollback armed as a backstop)
  3. impact        : which endpoints start failing (blast radius) and which containers are down
  4. detection     : kill-switch rule = error rate > max(5 %, 3x baseline) for 2 consecutive 5 s windows
  5. fix           : POST /api/v1/rollback/<intent> (docker start) + restart `web` so nginx re-resolves the IP
  6. recovery      : error rate < 2 % for 2 consecutive windows; prints time-to-detect and time-to-recover
A JSON record of the run is written to runs/day1_<timestamp>.json for the findings report.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

LOCUST = "http://127.0.0.1:8089"
API = "http://127.0.0.1:8000"
WINDOW = 5.0


def http(method, url, timeout=30):
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body.strip().startswith(("{", "[")) else body)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def locust_rows():
    _, d = http("GET", f"{LOCUST}/stats/requests", timeout=10)
    rows = {r["name"]: (r["num_requests"], r["num_failures"]) for r in d["stats"]}
    p95 = next((r.get("response_time_percentile_0.95") for r in d["stats"] if r["name"] == "Aggregated"), None)
    return rows, p95


def window(prev, cur):
    """Per-window totals and the endpoints that failed in this window."""
    req = cur["Aggregated"][0] - prev.get("Aggregated", (0, 0))[0]
    fail = cur["Aggregated"][1] - prev.get("Aggregated", (0, 0))[1]
    failing = sorted(
        n for n, (r, f) in cur.items()
        if n != "Aggregated" and f - prev.get(n, (0, 0))[1] > 0
    )
    return req, fail, (fail / req if req else 0.0), failing


def docker_down():
    """Target containers that are not running or not healthy."""
    out = subprocess.run(
        ["docker", "ps", "-a", "--filter", "label=com.chaospilot.role=target",
         "--format", "{{.Names}}|{{.Status}}"],
        capture_output=True, text=True,
    ).stdout.strip().splitlines()
    return sorted(n for n, s in (l.split("|", 1) for l in out if "|" in l)
                  if not s.startswith("Up") or "unhealthy" in s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="catalogue")
    ap.add_argument("--fault", default="container_crash")
    ap.add_argument("--ttl", type=int, default=180, help="backstop auto-rollback (s)")
    ap.add_argument("--baseline", type=int, default=30, help="steady-state seconds")
    ap.add_argument("--max-wait", type=int, default=120, help="give up detecting/recovering after (s)")
    a = ap.parse_args()

    run = {"target": a.target, "fault": a.fault, "windows": []}

    # 0. preflight
    code, root = http("GET", f"{API}/")
    if code != 200:
        sys.exit(f"controller not reachable on {API} ({code}): start it with docker compose -f controller/docker-compose.yml up -d")
    if docker_down():
        sys.exit(f"sandbox not healthy before the run: {docker_down()}  (fix that first)")

    # 1. baseline
    print(f"[1] baseline: {a.baseline}s of steady load")
    prev, _ = locust_rows()
    t0 = time.time()
    b_req = b_fail = 0
    while time.time() - t0 < a.baseline:
        time.sleep(WINDOW)
        cur, p95 = locust_rows()
        req, fail, rate, _ = window(prev, cur)
        b_req, b_fail, prev = b_req + req, b_fail + fail, cur
        print(f"    {time.time()-t0:5.0f}s  req={req:4d}  err={rate:6.2%}  p95={p95}ms")
    base_rate = b_fail / b_req if b_req else 0.0
    threshold = max(0.05, 3 * base_rate)
    run["baseline"] = {"requests": b_req, "error_rate": base_rate, "p95_ms": p95, "threshold": threshold}
    print(f"    baseline error rate {base_rate:.2%}, p95 {p95} ms -> kill-switch threshold {threshold:.0%}")

    # 2. inject
    print(f"[2] inject {a.fault} on '{a.target}' (TTL backstop {a.ttl}s)")
    code, res = http("POST", f"{API}/api/v1/inject?fault_type={a.fault}&target={a.target}&ttl_seconds={a.ttl}")
    if code != 200:
        sys.exit(f"inject failed ({code}): {res}")
    intent = res["result"]["intent_id"]
    t_inject = time.time()
    run["intent_id"], run["inject_result"] = intent, res["result"]
    print(f"    intent #{intent} running; rollback = {res['result'].get('rollback_command')}")

    # 3-4. observe until the kill-switch rule trips
    print(f"[3] observing (detect when err > {threshold:.0%} for 2 windows)")
    breaches, t_detect, affected, down_seen = 0, None, set(), set()
    while time.time() - t_inject < a.max_wait:
        time.sleep(WINDOW)
        cur, p95 = locust_rows()
        req, fail, rate, failing = window(prev, cur)
        prev = cur
        down = docker_down()
        affected.update(failing)
        down_seen.update(down)
        run["windows"].append({"t": round(time.time() - t_inject, 1), "phase": "fault", "req": req,
                               "err": rate, "p95": p95, "failing": failing, "down": down})
        print(f"    +{time.time()-t_inject:5.0f}s  err={rate:6.2%}  p95={p95}ms  down={down}  failing={failing}")
        breaches = breaches + 1 if rate > threshold else 0
        if breaches >= 2:
            t_detect = time.time()
            break
    if t_detect is None:
        print("    no breach detected within max-wait: the system absorbed this fault (that is a finding too)")

    # 5. fix
    print(f"[4] rollback intent #{intent} + restart web")
    code, rb = http("POST", f"{API}/api/v1/rollback/{intent}")
    run["rollback_result"] = rb
    print(f"    rollback HTTP {code}: verified={rb.get('result', {}).get('rollback_verified') if isinstance(rb, dict) else rb}")
    subprocess.run(["docker", "restart", "robotshop-web-1"], capture_output=True)
    t_fix = time.time()

    # 6. recovery
    print("[5] verifying recovery (err < 2% for 2 windows)")
    ok, t_rec = 0, None
    while time.time() - t_fix < a.max_wait:
        time.sleep(WINDOW)
        cur, p95 = locust_rows()
        req, fail, rate, failing = window(prev, cur)
        prev = cur
        down = docker_down()
        run["windows"].append({"t": round(time.time() - t_inject, 1), "phase": "recovery", "req": req,
                               "err": rate, "p95": p95, "failing": failing, "down": down})
        print(f"    +{time.time()-t_inject:5.0f}s  err={rate:6.2%}  p95={p95}ms  down={down}")
        ok = ok + 1 if (rate < 0.02 and not down and req > 0) else 0
        if ok >= 2:
            t_rec = time.time()
            break

    peak = max((w["err"] for w in run["windows"] if w["phase"] == "fault"), default=0.0)
    run["summary"] = {
        "services_down": sorted(down_seen),
        "endpoints_failing": sorted(affected),
        "peak_error_rate": peak,
        "time_to_detect_s": round(t_detect - t_inject, 1) if t_detect else None,
        "time_to_recover_s": round(t_rec - t_fix, 1) if t_rec else None,
        "recovered": t_rec is not None,
    }
    os.makedirs("runs", exist_ok=True)
    path = os.path.join("runs", time.strftime("day1_%Y%m%d_%H%M%S.json"))
    with open(path, "w") as f:
        json.dump(run, f, indent=2, default=str)

    s = run["summary"]
    print("\n==== RESULT ====")
    print(f"fault            : {a.fault} on {a.target} (intent #{intent})")
    print(f"services down    : {s['services_down']}")
    print(f"endpoints failing: {s['endpoints_failing']}")
    print(f"peak error rate  : {s['peak_error_rate']:.1%}  (baseline {base_rate:.2%})")
    print(f"time to detect   : {s['time_to_detect_s']} s")
    print(f"time to recover  : {s['time_to_recover_s']} s after rollback")
    print(f"recovered        : {s['recovered']}")
    print(f"saved            : {path}")
    sys.exit(0 if s["recovered"] else 1)


if __name__ == "__main__":
    main()
