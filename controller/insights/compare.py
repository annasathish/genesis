"""Run-to-run improvement: the answer to "how do you know 50 -> 30 -> 20 -> 10 is a real improvement?"

Rules (also printed on the dashboard and in docs/INSIGHTS.md):
  1. Only like-for-like runs are compared: same fault, same target, same parameters ("signature").
  2. For every KPI we report the absolute delta and the relative change vs the previous run AND vs the first run:
         relative_change = (old - new) / old        (positive = better for lower-is-better KPIs)
     e.g. error 50 -> 30 -> 20 -> 10 is -40 %, -33 %, -50 % step by step, and -80 % cumulative.
  3. A step only counts as "improved" / "regressed" if it is real, not noise:
       - the fault-phase error rate change passes a two-proportion z-test at p < 0.05 (failures / requests), and
       - the primary KPI (error-minutes) moved by at least MIN_EFFECT (10 %).
     If the two disagree in direction (shorter outage but a higher error rate while it lasted) the verdict
     is "mixed" and no improvement is claimed.
     Time KPIs (detect / recover) are measured in 5 s windows, so changes smaller than one window are reported
     as "within measurement resolution".
  4. Each run can carry a label ("after adding retries"), so every step is tied to the change that caused it.
"""
import math
from typing import Any, Dict, List, Optional

from insights.metrics import run_metrics

MIN_EFFECT = 0.10
ALPHA = 0.05

# KPI -> (direction, unit). direction "down" = lower is better.
KPIS = {
    "error_minutes": ("down", "min"),
    "peak_error_rate": ("down", "ratio"),
    "fault_error_rate": ("down", "ratio"),
    "failed_requests": ("down", "count"),
    "checkout_loss_min": ("down", "min"),
    "checkout_peak_loss": ("down", "ratio"),
    "slowest_endpoint_ms": ("down", "ms"),
    "time_to_detect_s": ("down", "s"),
    "time_to_recover_s": ("down", "s"),
    "blast_radius_endpoints": ("down", "count"),
    "resilience_score": ("up", "/10"),
}
TIME_KPIS = {"time_to_detect_s", "time_to_recover_s"}
PRIMARY = "error_minutes"


def two_proportion_z(f1: int, n1: int, f2: int, n2: int) -> Dict[str, Optional[float]]:
    """H0: both runs have the same failure probability. Returns z and two-sided p."""
    if n1 <= 0 or n2 <= 0:
        return {"z": None, "p_value": None}
    p1, p2 = f1 / n1, f2 / n2
    pooled = (f1 + f2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return {"z": 0.0, "p_value": 1.0}
    z = (p1 - p2) / se
    p = math.erfc(abs(z) / math.sqrt(2))
    return {"z": round(z, 3), "p_value": float(f"{p:.3g}")}  # 3 significant figures: tiny p stays non-zero


def kpi_delta(name: str, old: Any, new: Any, window_s: float = 5.0) -> Dict[str, Any]:
    direction, unit = KPIS[name]
    if old is None or new is None:
        return {"kpi": name, "old": old, "new": new, "delta": None, "relative_change": None,
                "better": None, "unit": unit, "note": "not measured in one of the runs"}
    delta = new - old
    rel = None if old == 0 else (old - new) / abs(old) if direction == "down" else (new - old) / abs(old)
    better = (delta < 0) if direction == "down" else (delta > 0)
    note = ""
    if name in TIME_KPIS and abs(delta) < window_s:
        better, note = None, "within measurement resolution (one window)"
    elif delta == 0:
        better = None
    return {"kpi": name, "old": old, "new": new, "delta": round(delta, 4),
            "relative_change": None if rel is None else round(rel, 4),
            "better": better, "unit": unit, "note": note}


def compare_pair(a: Dict[str, Any], b: Dict[str, Any], window_s: float = 5.0) -> Dict[str, Any]:
    """a = older run metrics, b = newer run metrics."""
    deltas = {k: kpi_delta(k, a.get(k), b.get(k), window_s) for k in KPIS}
    sig = two_proportion_z(a["fault_failures"], a["fault_requests"], b["fault_failures"], b["fault_requests"])
    primary = deltas[PRIMARY]
    effect = primary["relative_change"]
    if effect is None and primary["old"] == 0 and primary["new"] is not None:
        effect = -1.0 if primary["new"] > 0 else 0.0   # from zero errors to some errors is a regression, not "n/a"
    significant = sig["p_value"] is not None and sig["p_value"] < ALPHA
    err_better = b["fault_error_rate"] < a["fault_error_rate"]
    if effect is None or not significant or abs(effect) < MIN_EFFECT:
        verdict = "no significant change"
    elif (effect > 0) != err_better:
        # e.g. a shorter outage with a higher error rate while it lasted: do not claim either way
        verdict = "mixed"
    else:
        verdict = "improved" if effect > 0 else "regressed"
    return {
        "from": a["run_id"], "to": b["run_id"],
        "from_label": a["label"], "to_label": b["label"],
        "verdict": verdict,
        "primary_kpi": PRIMARY,
        "primary_relative_change": effect,
        "significance": {**sig, "alpha": ALPHA, "significant": significant,
                         "test": "two-proportion z-test on fault-phase failures/requests"},
        "deltas": deltas,
    }


BASELINE_PHASE = "baseline (no change)"
REVERTED_PHASE = "fix removed (back to baseline)"


def phase_of(label: str) -> str:
    """Runs labelled "fix: ..." mark a change to the system; every other run is the unchanged baseline."""
    lab = (label or "").strip()
    return lab if lab.lower().startswith("fix") else BASELINE_PHASE


def _pooled(ms: List[Dict[str, Any]], phase: str) -> Dict[str, Any]:
    """One pseudo-run per phase: KPI means, summed fault-phase counts (for the z-test)."""
    def avg(k):
        vals = [m[k] for m in ms if m.get(k) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None
    em = [m["error_minutes"] for m in ms]
    return {"run_id": f"{phase} ({len(ms)} run{'s' if len(ms) > 1 else ''})", "label": phase, "phase": phase,
            "runs": [m["run_id"] for m in ms], "n_runs": len(ms),
            "fault_failures": sum(m["fault_failures"] for m in ms), "fault_requests": sum(m["fault_requests"] for m in ms),
            "noise_band": {"min": min(em), "max": max(em)} if len(ms) > 1 else None,
            **{k: avg(k) for k in KPIS}}


def fix_effects(ms: List[Dict[str, Any]], window_s: float = 5.0) -> Dict[str, Any]:
    """Consecutive phases (baseline, fix A, fix B ...) compared with all their runs pooled."""
    segs: List[List[Dict[str, Any]]] = []
    for m in ms:
        if segs and phase_of(segs[-1][-1]["label"]) == phase_of(m["label"]):
            segs[-1].append(m)
        else:
            segs.append([m])
    phases = []
    for i, seg in enumerate(segs):
        name = phase_of(seg[0]["label"])
        if name == BASELINE_PHASE and i > 0:
            name = REVERTED_PHASE   # back to no change after a fix: "fix removed", not a second baseline
        phases.append(_pooled(seg, name))
    effects = []
    for a, b in zip(phases, phases[1:]):
        c = compare_pair(a, b, window_s)
        effects.append({**c, "from_phase": a["phase"], "to_phase": b["phase"],
                        "from_runs": a["n_runs"], "to_runs": b["n_runs"]})
    run_phase = {m["run_id"]: ph["phase"] for seg, ph in zip(segs, phases) for m in seg}
    return {"phases": phases, "effects": effects, "run_phase": run_phase}


def compare_runs(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    metrics = [run_metrics(r) for r in runs]
    window_by_id = {r["run_id"]: r["window_s"] for r in runs}
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for m in metrics:
        groups.setdefault(m["signature"], []).append(m)

    out = []
    for sig, ms in groups.items():
        steps = [compare_pair(ms[i - 1], ms[i], window_by_id[ms[i]["run_id"]]) for i in range(1, len(ms))]
        cumulative = compare_pair(ms[0], ms[-1], window_by_id[ms[-1]["run_id"]]) if len(ms) > 1 else None
        out.append({
            "signature": sig,
            "fault": ms[0]["fault"],
            "target": ms[0]["target"],
            "runs": [m["run_id"] for m in ms],
            "steps": steps,
            "cumulative": cumulative,
            **(fx := fix_effects(ms, window_by_id[ms[-1]["run_id"]])),
            "trend": [{"run_id": m["run_id"], "label": m["label"], "phase": fx["run_phase"][m["run_id"]],
                       "started_at": m["started_at"], **{k: m.get(k) for k in KPIS}} for m in ms],
        })
    return {"metrics": metrics, "groups": out, "method": METHOD}


METHOD = {
    "comparability": "Runs are grouped by signature = fault + target + parameters; only runs in the same group "
                     "are compared.",
    "relative_change": "(old - new) / old for lower-is-better KPIs, (new - old) / old for the score. Reported vs "
                       "the previous run (step) and vs the first run (cumulative).",
    "primary_kpi": "error_minutes = sum(window error rate x window length) / 60: outage-equivalent minutes, "
                   "combines severity and duration.",
    "significance": f"Two-proportion z-test on fault-phase failures/requests, alpha = {ALPHA}. A step is "
                    f"'improved' or 'regressed' only if significant AND error-minutes moved >= {int(MIN_EFFECT*100)} % in the same "
                    "direction as the error rate; if they disagree the step is 'mixed'.",
    "resolution": "Detect/recover times are sampled every 5 s; differences under one window are not claimed.",
    "attribution": "Label each run (--label) with the change made before it, so every step names its cause.",
    "fix_effect": "Runs labelled 'fix: ...' start a new phase. Each phase is compared with the one before it with all "
                  "runs pooled (mean KPIs, summed failures/requests for the z-test), so one noisy run cannot fake or "
                  "hide an improvement. Repeated runs with no change show the run-to-run noise band.",
}
