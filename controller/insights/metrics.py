"""Per-run KPIs, computed only from what the run recorded (no estimates unless flagged).

Definitions (each window is `window_s` seconds of Locust traffic, 5 s by default):
  error_rate_w        failures_w / requests_w
  peak_error_rate     max error_rate_w during the fault phase
  fault_error_rate    sum(failures) / sum(requests) over the fault phase (pooled, used for significance tests)
  error_minutes       sum(error_rate_w * window_s) / 60 over fault + recovery windows.
                      "Outage-equivalent minutes": 1 min at 100 % errors == 2 min at 50 %. This is the single
                      number that combines how bad (severity) and how long (duration) an incident was.
  degraded_s          seconds with error_rate_w >= 2 % (the recovery bar used by day1_demo.py)
  checkout_loss_min   like error_minutes but for the business flow only: sum(loss_w * window_s) / 60 where
                      loss_w = 1 - successful_checkouts_w / expected_checkouts_w (expected = baseline rate).
                      Needs per-endpoint data (v2 runs). v1 runs fall back to the aggregate error rate and are
                      flagged checkout_method="estimated_from_aggregate_errors".
  resilience score    controller/scorer.py (M5), unchanged, so the dashboard and REPORT.md agree.
"""
from typing import Any, Dict, Optional

from scorer import compute_resilience_score

CHECKOUT_ENDPOINT = "payment:pay"   # end-to-end checkout SLI (docs/M1_PLATFORM.md)
DEGRADED_ERR = 0.02


def _ok(ep: Optional[Dict[str, Any]], name: str) -> Optional[float]:
    if not ep or name not in ep:
        return None
    req, fail = ep[name]
    return float(req) - float(fail)


def checkout_loss(run: Dict[str, Any]) -> Dict[str, Any]:
    windows = run["windows"]
    win = run["window_s"]
    base = run["baseline"]
    base_ok = _ok(base.get("ep"), CHECKOUT_ENDPOINT)
    base_dur = base.get("duration_s")

    if base_ok is not None and base_dur and run["format"] == "v2":
        expected_per_window = base_ok / base_dur * win
        lost = 0.0
        loss_s = 0.0
        for w in windows:
            ok = _ok(w["ep"], CHECKOUT_ENDPOINT) or 0.0
            if expected_per_window <= 0:
                continue
            frac = min(1.0, max(0.0, 1.0 - ok / expected_per_window))
            loss_s += frac * win
            lost += max(0.0, expected_per_window - ok)
        return {
            "checkout_method": "measured",
            "checkout_loss_min": round(loss_s / 60, 3),
            "sandbox_checkouts_lost": round(lost, 1),
            "baseline_checkouts_per_min": round(base_ok / base_dur * 60, 2),
        }

    loss_s = sum(w["err"] * win for w in windows)
    return {
        "checkout_method": "estimated_from_aggregate_errors",
        "checkout_loss_min": round(loss_s / 60, 3),
        "sandbox_checkouts_lost": None,
        "baseline_checkouts_per_min": None,
    }


def run_metrics(run: Dict[str, Any]) -> Dict[str, Any]:
    win = run["window_s"]
    windows = run["windows"]
    fault = [w for w in windows if w["phase"] == "fault"]
    s = run["summary"]

    fault_req = sum(w["req"] for w in fault)
    fault_fail = sum(w["fail"] for w in fault)
    p95s = [w["p95"] for w in fault if isinstance(w["p95"], (int, float))]
    p95_base = run["baseline"].get("p95_ms") or 0.0
    p95_peak = max(p95s) if p95s else p95_base
    fault_err = fault_fail / fault_req if fault_req else 0.0
    ttd = s.get("time_to_detect_s")
    ttr = s.get("time_to_recover_s")

    score = compute_resilience_score(
        p95_during=p95_peak,
        p95_baseline=p95_base,
        error_rate=fault_err,
        recovery_time_sec=ttr,
        kill_switch_fired=ttd is not None,
    )
    degraded_s = sum(win for w in windows if w["err"] >= DEGRADED_ERR)
    incident_s = (max((w["t"] for w in windows if w["err"] >= DEGRADED_ERR), default=0.0)
                  + (win if degraded_s else 0.0))

    return {
        "run_id": run["run_id"],
        "started_at": run["started_at"],
        "label": run["label"],
        "signature": run["signature"],
        "fault": run["fault"],
        "target": run["target"],
        "format": run["format"],
        "baseline_error_rate": run["baseline"]["error_rate"],
        "baseline_p95_ms": p95_base,
        "peak_error_rate": round(max((w["err"] for w in fault), default=0.0), 4),
        "fault_error_rate": round(fault_err, 4),
        "fault_requests": fault_req,
        "fault_failures": fault_fail,
        "failed_requests": sum(w["fail"] for w in windows),
        "error_minutes": round(sum(w["err"] * win for w in windows) / 60, 3),
        "degraded_s": degraded_s,
        "incident_s": round(incident_s, 1),
        "p95_peak_ms": p95_peak,
        "time_to_detect_s": ttd,
        "time_to_recover_s": ttr,
        "recovered": bool(s.get("recovered")),
        "blast_radius_endpoints": len(s.get("endpoints_failing", [])),
        "endpoints_failing": s.get("endpoints_failing", []),
        "services_down": s.get("services_down", []),
        "resilience_score": score["score_out_of_10"],
        "score_breakdown": {k: score[k] for k in ("latency_score", "error_score", "recovery_score")},
        **checkout_loss(run),
    }
