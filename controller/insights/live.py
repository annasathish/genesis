"""Live system state for the dashboard, from real sources only.

    Locust        /stats/requests   throughput, per-window error rate (delta of counters), rolling p95, per endpoint
    Controller    /api/v1/intents/active   faults currently injected by the ChaosManager
    Toxiproxy     /proxies          network toxics currently attached
    Docker        docker ps         container state + health of every com.chaospilot.role=target container
    Kill switch   controller/baseline.json thresholds, evaluated on the live windows with the M4 rule
                  (breach = error rate > max_error_rate OR p95 > p95_latency_threshold_ms, for N consecutive windows)

If a source is unreachable its section says so (`ok: false`); nothing is ever filled in with made-up values.
Sampling is rate-limited (one sample per MIN_INTERVAL_S however many browsers poll), so load stays negligible.
"""
import json
import os
import subprocess
import urllib.error
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

LOCUST_URL = os.environ.get("LOCUST_HOST_URL", "http://127.0.0.1:8089")
SHOP_URL = os.environ.get("SHOP_URL", "http://127.0.0.1:8080")
# Health endpoints reached through the shop's own gateway (web/nginx), i.e. the path a shopper's browser uses.
PROBES = {"web": "/", "catalogue": "/api/catalogue/health", "user": "/api/user/health", "cart": "/api/cart/health",
          "shipping": "/api/shipping/health", "ratings": "/api/ratings/_health", "payment": "/api/payment/health"}
PROBE_TIMEOUT_S = 2.0
CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://127.0.0.1:8000")
TOXIPROXY_URL = os.environ.get("TOXIPROXY_HOST_URL", "http://127.0.0.1:8474")
BASELINE_FILE = Path(__file__).resolve().parents[1] / "baseline.json"
MIN_INTERVAL_S = 3.0
HISTORY = 100  # samples kept (~5 min at 3 s)

# Who calls whom (caller -> dependency). From the merged compose config (depends_on + *_HOST/URL env) plus
# the app's hard-coded defaults (payment -> cart/user, ratings -> catalogue: seen failing when catalogue crashed).
EDGES = [
    ("web", "catalogue"), ("web", "user"), ("web", "cart"), ("web", "shipping"), ("web", "ratings"), ("web", "payment"),
    ("catalogue", "mongodb"), ("user", "mongodb"), ("user", "redis"),
    ("cart", "redis"), ("cart", "catalogue"),
    ("shipping", "mysql"), ("shipping", "cart"),
    ("ratings", "mysql"), ("ratings", "catalogue"),
    ("payment", "rabbitmq"), ("payment", "payment-gateway"), ("payment", "cart"), ("payment", "user"),
    ("dispatch", "rabbitmq"),
]
TIERS = {
    "gateway": ["web"],
    "service": ["catalogue", "user", "cart", "shipping", "ratings", "payment", "dispatch"],
    "data": ["mongodb", "redis", "mysql", "rabbitmq", "payment-gateway"],
}
SERVICES = [s for tier in TIERS.values() for s in tier]


def callers_of(service: str) -> List[str]:
    """Services that break (directly or transitively) if `service` fails, by the dependency graph."""
    out, frontier = [], [service]
    while frontier:
        dep = frontier.pop()
        for caller, d in EDGES:
            if d == dep and caller not in out and caller != service:
                out.append(caller)
                frontier.append(caller)
    return out


def _get_json(url: str, timeout: float = 2.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _load_thresholds() -> Dict[str, Any]:
    try:
        t = json.loads(BASELINE_FILE.read_text(encoding="utf-8")).get("kill_switch_thresholds", {})
    except (OSError, ValueError):
        t = {}
    return {
        "max_error_rate": float(t.get("max_error_rate", 0.05)),
        "p95_latency_threshold_ms": float(t.get("p95_latency_threshold_ms", 0) or 0) or None,
        "consecutive_breach_windows": int(t.get("consecutive_breach_windows", 2)),
        "source": "controller/baseline.json" if t else "defaults (no baseline captured yet)",
    }


class LiveMonitor:
    def __init__(self, locust_url=LOCUST_URL, controller_url=CONTROLLER_URL, toxiproxy_url=TOXIPROXY_URL,
                 docker_cmd=("docker",), shop_url=SHOP_URL):
        self.locust_url, self.controller_url, self.toxiproxy_url = locust_url, controller_url, toxiproxy_url
        self.shop_url = shop_url.rstrip("/")
        self.docker_cmd = list(docker_cmd)
        self.history: deque = deque(maxlen=HISTORY)
        self._prev: Optional[Dict[str, Any]] = None
        self._last: Optional[Dict[str, Any]] = None
        self._last_at = 0.0
        self._breaches = 0
        self._last_trip_at: Optional[float] = None
        self._lock = threading.Lock()
        # last outage per service (probe failed or container not up), kept after the automatic rollback so the
        # proof is still on screen when the evaluator looks: {svc: {start, end, probe, container}}
        self.outages: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------ sources
    def _locust(self) -> Dict[str, Any]:
        try:
            d = _get_json(f"{self.locust_url}/stats/requests")
        except Exception as exc:  # noqa: BLE001 - any failure = source down
            return {"ok": False, "error": type(exc).__name__}
        rows = {r["name"]: (r["num_requests"], r["num_failures"]) for r in d.get("stats", [])}
        p95 = (d.get("current_response_time_percentiles") or {}).get("response_time_percentile_0.95")
        return {"ok": True, "rows": rows, "rps": d.get("total_rps"), "p95_ms": p95, "users": d.get("user_count"),
                "state": d.get("state")}

    def _intents(self) -> Dict[str, Any]:
        try:
            d = _get_json(f"{self.controller_url}/api/v1/intents/active")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": type(exc).__name__, "active": []}
        items = d if isinstance(d, list) else d.get("intents", d.get("active", []))
        active = [{"id": i.get("id") or i.get("intent_id"), "fault": i.get("fault_type"), "target": i.get("target_service")
                   or i.get("target"), "expires_at": i.get("expires_at")} for i in items if isinstance(i, dict)]
        return {"ok": True, "active": active}

    def _safety(self) -> Dict[str, Any]:
        """The controller's own kill switch + injection gate (GET /api/v1/safety/status, newer controllers)."""
        try:
            d = _get_json(f"{self.controller_url}/api/v1/safety/status")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": type(exc).__name__}
        return {"ok": True, "state": d.get("kill_switch_state"), "reason": d.get("kill_switch_reason"),
                "is_safe_to_inject": d.get("is_safe_to_inject"), "safety_reason": d.get("safety_reason"),
                "baseline_loaded": d.get("baseline_loaded"), "telemetry_age_s": d.get("telemetry_age_seconds"),
                "thresholds": d.get("thresholds")}

    def _probes(self) -> Dict[str, Any]:
        """GET each service's health endpoint via the shop gateway, in parallel. A crashed service shows up as a
        5xx from nginx or a timeout, independent of Locust's numbers."""
        from concurrent.futures import ThreadPoolExecutor

        def one(item):
            svc, path = item
            t0 = time.time()
            try:
                with urllib.request.urlopen(self.shop_url + path, timeout=PROBE_TIMEOUT_S) as r:
                    code, err = r.status, None
            except urllib.error.HTTPError as e:
                code, err = e.code, None
            except Exception as e:  # noqa: BLE001 - timeout / refused = down
                code, err = None, "timeout" if "timed out" in str(e) else type(e).__name__
            return svc, {"path": path, "status": code, "ms": round((time.time() - t0) * 1000), "error": err,
                         "ok": code is not None and code < 500 and err is None}
        with ThreadPoolExecutor(max_workers=len(PROBES)) as pool:
            return dict(pool.map(one, PROBES.items()))

    def _toxics(self) -> Dict[str, Any]:
        try:
            d = _get_json(f"{self.toxiproxy_url}/proxies")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": type(exc).__name__, "active": []}
        active = [{"proxy": name, "toxic": t.get("type"), "attributes": t.get("attributes", {})}
                  for name, p in d.items() for t in p.get("toxics", [])]
        return {"ok": True, "active": active}

    def _containers(self) -> Dict[str, Any]:
        fmt = '{{.Label "com.docker.compose.service"}}|{{.State}}|{{.Status}}'
        try:
            out = subprocess.run(self.docker_cmd + ["ps", "-a", "--filter", "label=com.chaospilot.role=target",
                                                    "--format", fmt], capture_output=True, text=True, timeout=4)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": type(exc).__name__, "services": {}}
        if out.returncode != 0:
            return {"ok": False, "error": "docker ps failed", "services": {}}
        services = {}
        for line in out.stdout.strip().splitlines():
            svc, state, status = (line.split("|", 2) + ["", ""])[:3]
            health = "unhealthy" if "unhealthy" in status else "healthy" if "healthy" in status else "n/a"
            services[svc] = {"state": state, "health": health, "status": status,
                             "up": state == "running" and health != "unhealthy"}
        return {"ok": True, "services": services}

    # ------------------------------------------------------------------ sampling
    def sample(self, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            if self._last and not force and now - self._last_at < MIN_INTERVAL_S:
                return self._last
            snap = self._build(now)
            self._last, self._last_at = snap, now
            return snap

    def _build(self, now: float) -> Dict[str, Any]:
        loc, intents, toxics, cont = self._locust(), self._intents(), self._toxics(), self._containers()
        safety = self._safety()
        probes = self._probes()
        thresholds = _load_thresholds()
        window = None
        if loc["ok"]:
            if self._prev and self._prev["rows"]:
                window = self._window(self._prev["rows"], loc["rows"], now - self._prev["t"])
            self._prev = {"rows": loc["rows"], "t": now}
        if window:
            window["p95_ms"] = loc["p95_ms"]
            window["rps"] = loc["rps"]
            breach = window["err"] > thresholds["max_error_rate"] or (
                thresholds["p95_latency_threshold_ms"] is not None and (loc["p95_ms"] or 0) > thresholds["p95_latency_threshold_ms"])
            self._breaches = self._breaches + 1 if breach else 0
            if self._breaches >= thresholds["consecutive_breach_windows"]:
                self._last_trip_at = now
            self.history.append({"ts": round(now, 1), **{k: window[k] for k in ("err", "rps", "p95_ms", "req")}})

        tripped = self._breaches >= thresholds["consecutive_breach_windows"]
        if safety["ok"] and safety.get("state") == "TRIPPED":
            tripped = True
        down = sorted(s for s, v in cont.get("services", {}).items() if not v["up"])
        recent_trip = self._last_trip_at is not None and now - self._last_trip_at < 60
        if intents["active"]:
            a = intents["active"][0]
            status = {"level": "injecting", "text": f"INJECTING FAILURE: {a['fault']} on {a['target']}"}
        elif toxics["active"]:
            status = {"level": "injecting", "text": f"NETWORK FAULT ACTIVE: {toxics['active'][0]['toxic']} on "
                                                    f"{toxics['active'][0]['proxy']}"}
        elif down:
            status = {"level": "degraded", "text": f"DEGRADED: {', '.join(down)} down"}
        elif tripped or recent_trip or (window and window["err"] > 0.02):
            status = {"level": "recovering", "text": "SYSTEM RECOVERING"}
        elif not loc["ok"]:
            status = {"level": "unknown", "text": "NO TELEMETRY (Locust unreachable)"}
        else:
            status = {"level": "stable", "text": "SYSTEM STABLE"}

        services = {}
        for s in SERVICES:
            c = cont.get("services", {}).get(s)
            ep = (window or {}).get("services", {}).get(s)
            pr = probes.get(s)
            web_pr = probes.get("web")
            # every probe goes through the shop's gateway (web): while web itself is down/restarting, the other
            # services' probes fail too, but that is web's outage, not theirs (unless their own container stopped)
            gateway_down = s != "web" and bool(web_pr and not web_pr["ok"])
            probe_down = bool(pr and not pr["ok"]) and not gateway_down
            down_now = bool(probe_down or (c and not c["up"]))
            out = self.outages.get(s)
            if down_now:
                if not out or out.get("end") is not None:
                    out = self.outages[s] = {"start": round(now, 1), "end": None, "probe": pr,
                                             "container": c and c["status"]}
                elif pr and not pr["ok"]:
                    out["probe"] = pr           # keep the latest failing answer while it is down
                if c and not c["up"]:
                    out["container"] = c["status"]
            elif out and out.get("end") is None:
                out["end"] = round(now, 1)
            services[s] = {
                "container": c,
                "live_error_rate": ep["err"] if ep else None,
                "live_requests": ep["req"] if ep else None,
                "probe": pr,
                "last_outage": dict(self.outages[s]) if s in self.outages else None,
                "gateway_down": gateway_down,
                "failing": bool((c and not c["up"]) or (ep and ep["req"] and ep["err"] > 0.05) or probe_down),
            }
        return {
            "ts": round(now, 1),
            "status": status,
            "window": window and {k: window[k] for k in ("err", "req", "fail", "rps", "p95_ms", "seconds")},
            "kill_switch": {"breach_windows": self._breaches, "tripped": tripped, **thresholds,
                            "controller": safety if safety["ok"] else None},
            "services": services,
            "edges": EDGES,
            "tiers": TIERS,
            "active_faults": intents["active"],
            "active_toxics": toxics["active"],
            "sources": {"locust": loc["ok"], "controller": intents["ok"], "toxiproxy": toxics["ok"], "docker": cont["ok"]},
            "users": loc.get("users"),
            "history": list(self.history),
        }

    @staticmethod
    def _window(prev: Dict[str, Any], cur: Dict[str, Any], seconds: float) -> Optional[Dict[str, Any]]:
        if "Aggregated" not in cur:
            return None
        req = cur["Aggregated"][0] - prev.get("Aggregated", (0, 0))[0]
        fail = cur["Aggregated"][1] - prev.get("Aggregated", (0, 0))[1]
        if req < 0:  # Locust stats were reset
            return None
        per_svc: Dict[str, List[int]] = {}
        for name, (r, f) in cur.items():
            if name == "Aggregated":
                continue
            pr, pf = prev.get(name, (0, 0))
            svc = name.split(":", 1)[0]
            acc = per_svc.setdefault(svc, [0, 0])
            acc[0] += max(0, r - pr)
            acc[1] += max(0, f - pf)
        return {
            "req": req, "fail": fail, "err": round(fail / req, 4) if req else 0.0, "seconds": round(seconds, 1),
            "services": {s: {"req": r, "err": round(f / r, 4) if r else 0.0} for s, (r, f) in per_svc.items()},
        }
