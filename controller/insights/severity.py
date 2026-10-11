"""One severity formula, shared by the vulnerability board (insights/vulnerability.py) and the Analyzer agent,
so the dashboard and the findings report always agree.

  severity (0-100) = 100 x (0.4 x error spike + 0.3 x min(1, incident s / 180) + 0.3 x impacted services / 7)
                     + up to 20 for slowdown: min(20, 20 x (latency ratio - 1) / 9)
  error spike      worst of the overall peak error rate and the peak checkout failure rate
  latency ratio    worst of overall p95 and the slowest endpoint's window mean, each vs its baseline
  level            Critical >= 60, High >= 40, Medium >= 20, Low otherwise
"""
from typing import Any, Dict, List

RECOVERY_CAP_S = 180.0
USER_FACING = ["web", "catalogue", "user", "cart", "shipping", "ratings", "payment"]
LEVELS = [(60, "Critical"), (40, "High"), (20, "Medium"), (0, "Low")]


def level(score: float) -> str:
    return next(name for lim, name in LEVELS if score >= lim)


def impacted_services(m: Dict[str, Any]) -> List[str]:
    return sorted({e.split(":", 1)[0] for e in m.get("endpoints_failing", [])})


def severity(m: Dict[str, Any]) -> Dict[str, Any]:
    spike = max(m["peak_error_rate"], m.get("checkout_peak_loss") or 0.0)
    rec = min(1.0, m["incident_s"] / RECOVERY_CAP_S)
    casc = min(1.0, len(impacted_services(m)) / len(USER_FACING))
    base = m.get("baseline_p95_ms") or 0
    ratio = (m.get("p95_peak_ms") or 0) / base if base else 1.0
    se = m.get("slowest_endpoint") or {}
    ratio = max(ratio, se.get("ratio") or 1.0)  # a slow checkout endpoint counts even if overall p95 hides it
    slow = min(20.0, 20.0 * max(0.0, ratio - 1) / 9) if ratio > 1 else 0.0
    score = min(100.0, 100 * (0.4 * spike + 0.3 * rec + 0.3 * casc) + slow)
    return {"severity_score": round(score, 1), "severity": level(score), "p95_ratio": round(ratio, 2),
            "components": {"error_spike": round(spike, 3), "checkout_peak_loss": m.get("checkout_peak_loss"),
                           "recovery": round(rec, 3), "cascade": round(casc, 3), "slowdown_points": round(slow, 1)}}
