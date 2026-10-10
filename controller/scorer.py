"""
M5 - Normalized Resilience Scoring Engine

Computes resilience scores on a strict [0, 1] scale (displayed as 0-10).
Kill Switch activation is tracked as a Platform Safety metric, NOT added
to the application resilience score.
"""

from typing import Optional


# ---------------------------------------------------------------------------
# Individual Scoring Components
# ---------------------------------------------------------------------------

def compute_latency_score(p95_during: float, p95_baseline: float) -> float:
    """
    Normalized [0, 1].  1 = no latency degradation, 0 = severe.

    Formula: max(0, 1 - min((p95_ratio - 1) / 4, 1))
    - If p95 stayed at baseline → ratio=1 → score=1
    - If p95 is 5× baseline    → ratio=5 → score=0
    """
    if p95_baseline <= 0:
        return 1.0
    p95_ratio = p95_during / p95_baseline
    return max(0.0, 1.0 - min((p95_ratio - 1) / 4, 1.0))


def compute_error_score(
    error_rate: float,
    max_tolerable_error: float = 0.10,
) -> float:
    """
    Normalized [0, 1].  1 = zero errors, 0 = at/above tolerance.

    Formula: max(0, 1 - (error_rate / max_tolerable_error))
    """
    if max_tolerable_error <= 0:
        return 0.0
    return max(0.0, 1.0 - (error_rate / max_tolerable_error))


def compute_recovery_score(
    recovery_time_sec: Optional[float],
    max_recovery_sec: float = 300.0,
) -> float:
    """
    Normalized [0, 1].  1 = instant recovery, 0 = took ≥ max_recovery_sec.

    Faster recovery (time from rollback until metrics stabilise within
    baseline bands for 3 consecutive windows) → closer to 1.
    """
    if recovery_time_sec is None:
        return 0.0
    if max_recovery_sec <= 0:
        return 0.0
    return max(0.0, 1.0 - min(recovery_time_sec / max_recovery_sec, 1.0))


# ---------------------------------------------------------------------------
# Composite Score
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS = {"latency": 0.40, "error": 0.40, "recovery": 0.20}


def compute_resilience_score(
    p95_during: float,
    p95_baseline: float,
    error_rate: float,
    recovery_time_sec: Optional[float],
    kill_switch_fired: bool,
    max_tolerable_error: float = 0.10,
    max_recovery_sec: float = 300.0,
    weights: Optional[dict] = None,
) -> dict:
    """
    Compute the full normalised resilience breakdown for one experiment.

    Returns a dict with individual sub-scores, the composite [0,1] score,
    a human-readable /10 score, and the Platform Safety metadata.
    """
    w = weights or DEFAULT_WEIGHTS

    lat = compute_latency_score(p95_during, p95_baseline)
    err = compute_error_score(error_rate, max_tolerable_error)
    rec = compute_recovery_score(recovery_time_sec, max_recovery_sec)

    composite = (
        w["latency"] * lat
        + w["error"] * err
        + w["recovery"] * rec
    )

    return {
        "latency_score": round(lat, 4),
        "error_score": round(err, 4),
        "recovery_score": round(rec, 4),
        "composite_score": round(composite, 4),
        "score_out_of_10": round(composite * 10, 2),
        "kill_switch_fired": kill_switch_fired,
        "platform_safety": {
            "kill_switch_triggered": kill_switch_fired,
            "note": (
                "Kill switch activation is logged as a Platform Safety "
                "metric and does not affect the app resilience score."
            ),
        },
    }
