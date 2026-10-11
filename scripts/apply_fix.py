"""Apply / revert a resilience fix in the sandbox, for the "re-run experiments after a fix" stretch goal.

    python scripts/apply_fix.py replicas --service catalogue --count 2   # fix: run 2 replicas
    python scripts/day1_demo.py --target catalogue --label "fix: catalogue x2 replicas"   # re-run the experiment
    python scripts/apply_fix.py revert --service catalogue               # back to 1 replica

Uses the same compose files as `make up` and only touches the named service (--no-deps --no-recreate), then
restarts `web` so nginx resolves every replica's IP (proxy_pass http://<service>:8080 is resolved at nginx start).
Waits until every replica and web report healthy before returning, so the next experiment's preflight passes.
"""
import argparse
import subprocess
import sys
import time

SB = ["docker", "compose", "-f", "sandbox/docker-compose.yaml", "-f", "sandbox/docker-compose.override.yml"]
SCALABLE = {"catalogue", "cart", "user", "shipping", "ratings", "payment", "dispatch"}  # stateless app services


def sh(cmd):
    print("  $ " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"command failed ({r.returncode}): {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def replicas(service):
    out = sh(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.service={service}",
              "--filter", "label=com.docker.compose.project=robotshop", "--format", "{{.Names}}|{{.Status}}"])
    return [line.split("|", 1) for line in out.strip().splitlines() if "|" in line]


def wait_healthy(service, want=None, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        reps = replicas(service)
        healthy = [n for n, s in reps if s.startswith("Up") and "(healthy)" in s]
        if reps and len(healthy) == len(reps) and (want is None or len(reps) == want):
            print(f"  {service}: {len(healthy)} healthy replica(s): {', '.join(sorted(healthy))}")
            return
        time.sleep(3)
    sys.exit(f"{service} not healthy after {timeout}s: {replicas(service)}")


def scale(service, count):
    if service not in SCALABLE:
        sys.exit(f"'{service}' is not a stateless app service; scalable: {sorted(SCALABLE)}")
    sh(SB + ["up", "-d", "--no-deps", "--no-recreate", "--scale", f"{service}={count}", service])
    wait_healthy(service, want=count)
    sh(["docker", "restart", "robotshop-web-1"])
    wait_healthy("web")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["replicas", "revert", "status"])
    ap.add_argument("--service", default="catalogue")
    ap.add_argument("--count", type=int, default=2)
    a = ap.parse_args()
    if a.action == "status":
        for n, s in replicas(a.service):
            print(f"  {n}: {s}")
        return
    if a.action == "replicas":
        if not 2 <= a.count <= 3:
            sys.exit("--count must be 2 or 3 (memory budget)")
        print(f"[fix] {a.service} -> {a.count} replicas")
        scale(a.service, a.count)
    else:
        print(f"[revert] {a.service} -> 1 replica")
        scale(a.service, 1)
    print("done")


if __name__ == "__main__":
    main()
