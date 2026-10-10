"""Day-1 smoke test for the Robot Shop sandbox. Standard library only.

Usage:  make verify   (or: python sandbox/verify.py)
Exit code 0 = everything the chaos controller depends on is reachable.
"""
import base64
import json
import subprocess
import sys
import urllib.error
import urllib.request

WEB = "http://127.0.0.1:8080"
TOXIPROXY = "http://127.0.0.1:8474"
RABBIT = "http://127.0.0.1:15672"
LOCUST = "http://127.0.0.1:8089"
EXPECTED_PROXIES = {
    "catalogue_to_mongodb", "user_to_mongodb", "cart_to_redis", "cart_to_catalogue",
    "shipping_to_mysql", "ratings_to_mysql", "shipping_to_cart", "payment_to_rabbitmq",
    "payment_to_gateway",
}
MEM_BUDGET_MB = 2500

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{' - ' + detail if detail else ''}")


def get(url, auth=None, timeout=5):
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def http_check(name, url, **kw):
    try:
        status, _ = get(url, **kw)
        check(name, status == 200, f"HTTP {status}")
    except (urllib.error.URLError, OSError) as e:
        check(name, False, str(e))


# 1. every service answers through the web gateway (same path real users take)
http_check("web index", f"{WEB}/")
http_check("catalogue via web", f"{WEB}/api/catalogue/categories")
http_check("user via web", f"{WEB}/api/user/uniqueid")
http_check("cart via web", f"{WEB}/api/cart/health")
http_check("shipping via web", f"{WEB}/api/shipping/codes")
http_check("ratings via web", f"{WEB}/api/ratings/_health")
http_check("payment via web", f"{WEB}/api/payment/health")

# 2. Toxiproxy has every edge we inject network faults on, with no toxic left over
try:
    _, body = get(f"{TOXIPROXY}/proxies")
    proxies = json.loads(body)
    missing = EXPECTED_PROXIES - set(proxies)
    check("toxiproxy proxies", not missing, f"missing: {sorted(missing)}" if missing else f"{len(proxies)} proxies")
    leftover = {n: [t["name"] for t in p.get("toxics", [])] for n, p in proxies.items() if p.get("toxics")}
    check("toxiproxy clean (no toxics)", not leftover, str(leftover) if leftover else "")
except (urllib.error.URLError, OSError, ValueError) as e:
    check("toxiproxy API", False, str(e))

# 3. RabbitMQ management API (queue depth = async health signal)
try:
    _, body = get(f"{RABBIT}/api/queues", auth="guest:guest")
    queues = {q["name"]: q for q in json.loads(body)}
    detail = ", ".join(f"{n}: {q.get('messages', 0)} msgs, {q.get('consumers', 0)} consumers" for n, q in queues.items())
    check("rabbitmq queues", bool(queues), detail or "no queues yet (dispatch declares 'orders' on start)")
except (urllib.error.URLError, OSError, ValueError) as e:
    check("rabbitmq management API", False, str(e))

# 4. Locust is generating load and the shop is serving it
try:
    _, body = get(f"{LOCUST}/stats/requests")
    stats = json.loads(body)
    total = next((s for s in stats.get("stats", []) if s.get("name") == "Aggregated"), {})
    reqs, fails = total.get("num_requests", 0), total.get("num_failures", 0)
    ratio = fails / reqs if reqs else 0.0
    check("locust load running", reqs > 0 and stats.get("state") in ("running", "spawning"),
          f"state={stats.get('state')} requests={reqs}")
    check("baseline error rate < 2%", reqs > 0 and ratio < 0.02, f"{ratio:.2%} ({fails}/{reqs})")
    pay = next((s for s in stats.get("stats", []) if s.get("name") == "payment:pay"), None)
    check("checkout flow completing", bool(pay and pay.get("num_requests", 0) > pay.get("num_failures", 0)),
          f"payment:pay {pay.get('num_requests', 0)} req / {pay.get('num_failures', 0)} fail" if pay else "no payment calls yet")
except (urllib.error.URLError, OSError, ValueError) as e:
    check("locust API", False, str(e))

# 5. memory footprint of the whole sandbox
try:
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    units = {"B": 1 / 1048576, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024}
    total_mb = 0.0
    rows = []
    for line in out.splitlines():
        name, usage = line.split("\t")
        if not name.startswith("robotshop-"):
            continue
        used = usage.split("/")[0].strip()
        for u, f in sorted(units.items(), key=lambda kv: -len(kv[0])):
            if used.endswith(u):
                mb = float(used[: -len(u)]) * f
                total_mb += mb
                rows.append((mb, name))
                break
    for mb, name in sorted(rows, reverse=True):
        print(f"       {name:<32} {mb:7.0f} MB")
    check(f"sandbox RAM under {MEM_BUDGET_MB} MB", 0 < total_mb < MEM_BUDGET_MB, f"{total_mb:.0f} MB total")
except (subprocess.SubprocessError, OSError, ValueError) as e:
    check("docker stats", False, str(e))

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
