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
A JSON record of the run is written to runs/day1_<timestamp>.json for the findings report and the
insights dashboard (`make dashboard`). Label runs with the change made before them, so run-to-run
improvement can be attributed:  python scripts/day1_demo.py --label "cart: cache catalogue products"
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
SHOP = "http://127.0.0.1:8080"
# health endpoint per service, reached through the shop's gateway (what a shopper's browser hits)
PROBES = {"web": "/", "catalogue": "/api/catalogue/health", "user": "/api/user/health", "cart": "/api/cart/health",
          "shipping": "/api/shipping/health", "ratings": "/api/ratings/_health", "payment": "/api/payment/health"}
LAT = {}  # last Locust snapshot: {endpoint: (requests, total response ms)}, set by locust_rows()


def http(method, url, timeout=30, body=None):
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(url, method=method, data=data,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body.strip().startswith(("{", "[")) else body)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def locust_rows():
    _, d = http("GET", f"{LOCUST}/stats/requests", timeout=10)
    rows = {r["name"]: (r["num_requests"], r["num_failures"]) for r in d["stats"]}
    LAT.clear()
    LAT.update({r["name"]: (r["num_requests"], (r.get("avg_response_time") or 0) * r["num_requests"])
                for r in d["stats"] if r["name"] != "Aggregated"})
    # rolling p95 (Locust's current window); the Aggregated row's percentile is cumulative since the load started,
    # so after tens of thousands of requests it barely moves during a fault
    p95 = (d.get("current_response_time_percentiles") or {}).get("response_time_percentile_0.95")
    if p95 is None:
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


def endpoint_deltas(prev, cur):
    """{endpoint: [requests, failures]} for this window (feeds the business-impact checkout measurement)."""
    return {n: [r - prev.get(n, (0, 0))[0], f - prev.get(n, (0, 0))[1]]
            for n, (r, f) in cur.items() if n != "Aggregated"}


def latency_deltas(prev_lat, cur_lat):
    """{endpoint: mean response ms in this window}, from the change in Locust's cumulative totals."""
    out = {}
    for n, (r, tot) in cur_lat.items():
        pr, pt = prev_lat.get(n, (0, 0.0))
        if r - pr > 0:
            out[n] = round((tot - pt) / (r - pr), 1)
    return out


def slow_endpoints(lat, base_lat, factor):
    """Endpoints whose window mean is above factor x their baseline mean (and above 200 ms)."""
    return sorted(n for n, ms in lat.items() if ms > 200 and n in base_lat and ms > factor * max(base_lat[n], 1.0))


def probe(service):
    """Evidence that a service is (un)reachable: GET its health endpoint through the shop gateway."""
    path = PROBES.get(service)
    if not path:
        return None
    t0 = time.time()
    try:
        with urllib.request.urlopen(SHOP + path, timeout=3) as r:
            code, err = r.status, None
    except urllib.error.HTTPError as e:
        code, err = e.code, None
    except Exception as e:  # noqa: BLE001 - refused / timeout = down
        code, err = None, "timeout" if "timed out" in str(e) else type(e).__name__
    return {"path": path, "status": code, "error": err, "ms": round((time.time() - t0) * 1000),
            "ok": code is not None and code < 500 and err is None}


def docker_down():
    """Target containers that are not running or not healthy."""
    out = subprocess.run(
        ["docker", "ps", "-a", "--filter", "label=com.chaospilot.role=target",
         "--format", "{{.Names}}|{{.Status}}"],
        capture_output=True, text=True,
    ).stdout.strip().splitlines()
    return sorted(n for n, s in (l.split("|", 1) for l in out if "|" in l)
                  if not s.startswith("Up") or "unhealthy" in s)


HEAL_AFTER_S = 15   # after rollback, a container still exited this long did not come back by itself


def exited_targets():
    """Target containers that have stopped (exited / dead), not just still starting."""
    out = subprocess.run(
        ["docker", "ps", "-a", "--filter", "label=com.chaospilot.role=target",
         "--format", "{{.Names}}|{{.Status}}"],
        capture_output=True, text=True,
    ).stdout.strip().splitlines()
    return sorted(n for n, s in (l.split("|", 1) for l in out if "|" in l)
                  if s.startswith(("Exited", "Dead", "Created")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="catalogue")
    ap.add_argument("--fault", default="container_crash")
    ap.add_argument("--ttl", type=int, default=180, help="backstop auto-rollback (s)")
    ap.add_argument("--baseline", type=int, default=30, help="steady-state seconds")
    ap.add_argument("--max-wait", type=int, default=120, help="give up detecting/recovering after (s)")
    ap.add_argument("--label", default="", help="what changed before this run, e.g. 'added retries to cart'")
    ap.add_argument("--params", default="{}", help='fault parameters as JSON, e.g. \'{"latency_ms": 2000}\'')
    ap.add_argument("--p95-factor", type=float, default=5.0, help="slowdown breach: p95 above this x baseline")
    a = ap.parse_args()
    try:
        params = json.loads(a.params)
    except ValueError:
        sys.exit(f"--params is not valid JSON: {a.params}")

    probe_svc = a.target.partition("_to_")[0] if "_to_" in a.target else a.target
    run = {"target": a.target, "fault": a.fault, "params": params, "label": a.label, "window_s": WINDOW,
           "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "windows": []}

    # 0. preflight
    code, root = http("GET", f"{API}/")
    if code != 200:
        sys.exit(f"controller not reachable on {API} ({code}): start it with docker compose -f controller/docker-compose.yml up -d")
    if docker_down():
        sys.exit(f"sandbox not healthy before the run: {docker_down()}  (fix that first)")

    # 1. baseline
    print(f"[1] baseline: {a.baseline}s of steady load")
    prev, _ = locust_rows()
    b_start, lat_start = prev, dict(LAT)
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
    p95_limit = max(200.0, a.p95_factor * (p95 or 0))  # M4 rule: p95 > 5x baseline is a breach too (slowdowns)
    run["baseline"] = {"requests": b_req, "error_rate": base_rate, "p95_ms": p95, "threshold": threshold,
                       "p95_limit_ms": p95_limit, "duration_s": round(time.time() - t0, 1),
                       "ep": endpoint_deltas(b_start, prev), "lat": latency_deltas(lat_start, LAT)}
    base_lat = run["baseline"]["lat"]
    prev_lat = dict(LAT)
    print(f"    baseline error rate {base_rate:.2%}, p95 {p95} ms -> breach if err > {threshold:.0%} "
          f"or p95 > {p95_limit:.0f} ms")

    # 2. inject
    print(f"[2] inject {a.fault} on '{a.target}' {params or ''} (TTL backstop {a.ttl}s)")
    # Controller API: JSON body {fault_type, target, params, ttl_seconds}; older controllers took query parameters.
    code, res = http("POST", f"{API}/api/v1/inject",
                     body={"fault_type": a.fault, "target": a.target, "params": params, "ttl_seconds": a.ttl})
    if code == 422:
        code, res = http("POST", f"{API}/api/v1/inject?fault_type={a.fault}&target={a.target}&ttl_seconds={a.ttl}",
                         body=params or None)
    if code == 409:  # the controller's safety gate refused the injection
        detail = res.get("detail", res) if isinstance(res, dict) else res
        reason = detail.get("safety_reason") if isinstance(detail, dict) else detail
        sys.exit(f"controller refused the injection (safety gate): {reason}")
    if code != 200:
        sys.exit(f"inject failed ({code}): {res}")
    intent = res["result"]["intent_id"]
    t_inject = time.time()
    run["intent_id"], run["inject_result"] = intent, res["result"]
    print(f"    intent #{intent} running; rollback = {res['result'].get('rollback_command')}")

    # 3-4. observe until the kill-switch rule trips
    print(f"[3] observing (detect when err > {threshold:.0%}, p95 > {p95_limit:.0f} ms or an endpoint is "
          f"{a.p95_factor:g}x slower than baseline, for 2 windows)")
    breaches, t_detect, affected, down_seen, slowed = 0, None, set(), set(), set()
    try:
        observe = True
        while observe and time.time() - t_inject < a.max_wait:
            time.sleep(WINDOW)
            cur, p95 = locust_rows()
            req, fail, rate, failing = window(prev, cur)
            ep = endpoint_deltas(prev, cur)
            lat, prev_lat = latency_deltas(prev_lat, LAT), dict(LAT)
            slow = slow_endpoints(lat, base_lat, a.p95_factor)
            prev = cur
            down = docker_down()
            affected.update(failing)
            slowed.update(slow)
            down_seen.update(down)
            run["windows"].append({"t": round(time.time() - t_inject, 1), "phase": "fault", "req": req,
                                   "err": rate, "p95": p95, "failing": failing, "down": down, "ep": ep,
                                   "lat": lat, "slow": slow, "probe": probe(probe_svc)})
            print(f"    +{time.time()-t_inject:5.0f}s  err={rate:6.2%}  p95={p95}ms  down={down}  failing={failing}"
                  + (f"  slow={slow}" if slow else ""))
            breaches = breaches + 1 if (rate > threshold or (p95 or 0) > p95_limit or slow) else 0
            if breaches >= 2:
                t_detect = time.time()
                observe = False
    except BaseException:
        # Ctrl+C or a crash mid-experiment: roll back now instead of waiting for the TTL backstop
        print(f"\n    interrupted: rolling back intent #{intent}")
        http("POST", f"{API}/api/v1/rollback/{intent}")
        raise
    if t_detect is None:
        print("    no breach detected within max-wait: the system absorbed this fault (that is a finding too)")

    # 5. fix
    crash = a.fault == "container_crash"
    print(f"[4] rollback intent #{intent}" + (" + restart web" if crash else ""))
    code, rb = http("POST", f"{API}/api/v1/rollback/{intent}")
    run["rollback_result"] = rb
    already = isinstance(rb, dict) and str((rb.get("result") or {}).get("details", "")).startswith("Intent already")
    run["rolled_back_by"] = "kill_switch_or_ttl" if already else "day1_demo"
    print(f"    rollback HTTP {code}: verified={rb.get('result', {}).get('rollback_verified') if isinstance(rb, dict) else rb}"
          + ("  (already rolled back by the controller's kill switch / TTL)" if already else ""))
    if crash:  # a restarted container can get a new IP: nginx in web must re-resolve it. Other faults keep IPs.
        subprocess.run(["docker", "restart", "robotshop-web-1"], capture_output=True)
    t_fix = time.time()

    # 6. recovery
    print(f"[5] verifying recovery (err < 2% and p95 < {p95_limit:.0f} ms for 2 windows)")
    ok, t_rec = 0, None
    run["auto_healed"] = []
    while time.time() - t_fix < a.max_wait:
        time.sleep(WINDOW)
        # self-healing: a service that crashed as a cascade (e.g. dispatch panics when RabbitMQ goes away and has no
        # restart policy) stays down after the rollback. Restart it and record it: that is a finding, not a pass.
        if time.time() - t_fix >= HEAL_AFTER_S:
            for name in exited_targets():
                if any(h["container"] == name for h in run["auto_healed"]):
                    continue
                subprocess.run(["docker", "start", name], capture_output=True)
                run["auto_healed"].append({"container": name, "t": round(time.time() - t_inject, 1)})
                print(f"    [auto-heal] {name} stayed down after the rollback (no self-recovery): docker start {name}")
        cur, p95 = locust_rows()
        req, fail, rate, failing = window(prev, cur)
        ep = endpoint_deltas(prev, cur)
        lat, prev_lat = latency_deltas(prev_lat, LAT), dict(LAT)
        slow = slow_endpoints(lat, base_lat, a.p95_factor)
        prev = cur
        down = docker_down()
        run["windows"].append({"t": round(time.time() - t_inject, 1), "phase": "recovery", "req": req,
                               "err": rate, "p95": p95, "failing": failing, "down": down, "ep": ep,
                               "lat": lat, "slow": slow, "probe": probe(probe_svc)})
        print(f"    +{time.time()-t_inject:5.0f}s  err={rate:6.2%}  p95={p95}ms  down={down}")
        ok = ok + 1 if (rate < 0.02 and not down and req > 0 and (p95 or 0) <= p95_limit and not slow) else 0
        if ok >= 2:
            t_rec = time.time()
            break

    peak = max((w["err"] for w in run["windows"] if w["phase"] == "fault"), default=0.0)
    run["summary"] = {
        "services_down": sorted(down_seen),
        "endpoints_failing": sorted(affected),
        "endpoints_slow": sorted(slowed),
        # first moment the target's own health endpoint failed through the shop: proof the service was down
        "evidence": next(({"t": w["t"], "service": probe_svc, **w["probe"]} for w in run["windows"]
                          if w["phase"] == "fault" and w.get("probe") and not w["probe"]["ok"]), None),
        "peak_error_rate": peak,
        "time_to_detect_s": round(t_detect - t_inject, 1) if t_detect else None,
        "time_to_recover_s": round(t_rec - t_fix, 1) if t_rec else None,
        "recovered": t_rec is not None,
        # containers the rollback alone did not bring back (they had to be restarted): a resilience finding
        "auto_healed": [h["container"] for h in run["auto_healed"]],
    }
    os.makedirs("runs", exist_ok=True)
    path = os.path.join("runs", time.strftime("day1_%Y%m%d_%H%M%S.json"))
    with open(path, "w") as f:
        json.dump(run, f, indent=2, default=str)

    s = run["summary"]
    print("\n==== RESULT ====")
    print(f"fault            : {a.fault} on {a.target} (intent #{intent})")
    print(f"services down    : {s['services_down']}")
    if s.get("evidence"):
        ev = s["evidence"]
        print(f"evidence         : GET {ev['path']} via the shop -> {ev['status'] or ev['error']} at +{ev['t']}s")
    print(f"endpoints failing: {s['endpoints_failing']}")
    print(f"peak error rate  : {s['peak_error_rate']:.1%}  (baseline {base_rate:.2%})")
    print(f"time to detect   : {s['time_to_detect_s']} s")
    print(f"time to recover  : {s['time_to_recover_s']} s after rollback")
    print(f"recovered        : {s['recovered']}")
    if s["auto_healed"]:
        print(f"auto-healed      : {', '.join(s['auto_healed'])} (did not recover by itself; restarted)")
    print(f"saved            : {path}")
    sys.exit(0 if s["recovered"] else 1)


if __name__ == "__main__":
    main()
