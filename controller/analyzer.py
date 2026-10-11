"""
M5 - LLM Report Analyzer (Strict Constraints)

Rules enforced by design:
  1. The LLM NEVER writes or hallucinates numeric metrics.
  2. Python extracts all numbers from the DB and injects them into the template.
  3. The LLM only provides:
       - A plain-text explanation of what happened and why.
       - A recommended fix chosen from the FIXED_VOCABULARY list.
"""

import json
from typing import Optional

from ai_client import generate_json

try:  # PII masking before anything leaves for the LLM (controller/insights/pii.py)
    from insights.pii import Masker
except ImportError:  # pragma: no cover - keep the analyzer usable on its own
    Masker = None

# ── Fixed Vocabulary ────────────────────────────────────────────────
FIXED_VOCABULARY = [
    "Increase Replicas",
    "Adjust Timeouts",
    "Add Retries",
    "Implement Circuit Breaker",
]

def analyze_experiment(
    fault_type: str,
    target_service: str,
    fault_params: str,
    p95_baseline: float,
    p95_during: float,
    error_rate: float,
    recovery_time_sec: Optional[float],
    kill_switch_fired: bool,
    score_out_of_10: float,
    use_llm: bool = True,
) -> dict:
    """
    Generate an analysis for a single experiment.

    Returns:
        {
            "explanation": str,
            "recommended_fix": str   # always from FIXED_VOCABULARY
        }
    """
    if not use_llm:
        return _heuristic_analysis(
            fault_type, target_service, p95_baseline, p95_during,
            error_rate, recovery_time_sec, kill_switch_fired,
        )

    try:
        return _llm_analysis(
            fault_type, target_service, fault_params,
            p95_baseline, p95_during, error_rate,
            recovery_time_sec, kill_switch_fired, score_out_of_10,
        )
    except Exception as exc:
        print(f"[M5-Analyzer] LLM analysis failed ({exc}), falling back to heuristics.")
        return _heuristic_analysis(
            fault_type, target_service, p95_baseline, p95_during,
            error_rate, recovery_time_sec, kill_switch_fired,
        )


# ── LLM Path ───────────────────────────────────────────────────────

def _llm_analysis(
    fault_type: str,
    target_service: str,
    fault_params: str,
    p95_baseline: float,
    p95_during: float,
    error_rate: float,
    recovery_time_sec: Optional[float],
    kill_switch_fired: bool,
    score_out_of_10: float,
) -> dict:
    """Call Gemini with strict constraints."""
    prompt = f"""You are an expert Site-Reliability Engineer reviewing a chaos experiment.

STRICT RULES:
- Do NOT invent or repeat any numeric metrics. All numbers are already recorded.
- Provide ONLY a short plain-text explanation (2-4 sentences) of the likely
  root cause and impact.
- Choose exactly ONE recommended fix from this list: {json.dumps(FIXED_VOCABULARY)}.

Context (pre-extracted from the database — do NOT restate these numbers):
  Fault Type      : {fault_type}
  Target Service  : {target_service}
  Fault Parameters: {fault_params}
  Kill Switch     : {"FIRED" if kill_switch_fired else "did not fire"}
  Resilience Score: {score_out_of_10}/10

Respond in this exact JSON format (no markdown, no extra text):
{{"explanation": "...", "recommended_fix": "..."}}
"""

    if Masker is not None:
        prompt = Masker().mask_text(prompt)

    raw = generate_json(prompt).strip().removeprefix("```json").removesuffix("```").strip()
    parsed = json.loads(raw)
    if Masker is not None:
        parsed["explanation"] = Masker().mask_text(parsed.get("explanation", ""))

    # Enforce vocabulary constraint
    fix = parsed.get("recommended_fix", "")
    if fix not in FIXED_VOCABULARY:
        fix = _pick_heuristic_fix(fault_type, kill_switch_fired)
    parsed["recommended_fix"] = fix

    return {
        "explanation": parsed.get("explanation", ""),
        "recommended_fix": fix,
    }


# ── Heuristic Fallback ─────────────────────────────────────────────

def _heuristic_analysis(
    fault_type: str,
    target_service: str,
    p95_baseline: float,
    p95_during: float,
    error_rate: float,
    recovery_time_sec: Optional[float],
    kill_switch_fired: bool,
) -> dict:
    """Code-based analysis when LLM is unavailable or --no-llm is set."""
    fix = _pick_heuristic_fix(fault_type, kill_switch_fired)

    if kill_switch_fired:
        explanation = (
            f"The {fault_type} fault on '{target_service}' caused severe degradation, "
            f"triggering the Kill Switch. The service lacks sufficient redundancy "
            f"to absorb this class of failure."
        )
    elif error_rate > 0.05:
        explanation = (
            f"The {fault_type} fault on '{target_service}' elevated the error rate "
            f"beyond acceptable thresholds, indicating the service does not "
            f"gracefully handle this failure mode."
        )
    elif p95_baseline > 0 and p95_during / p95_baseline > 3:
        explanation = (
            f"The {fault_type} fault on '{target_service}' caused significant "
            f"latency amplification. The service continued responding but at "
            f"degraded performance levels."
        )
    else:
        explanation = (
            f"The {fault_type} fault on '{target_service}' was absorbed with "
            f"minimal impact, suggesting reasonable resilience for this failure mode."
        )

    return {"explanation": explanation, "recommended_fix": fix}


def _pick_heuristic_fix(fault_type: str, kill_switch_fired: bool) -> str:
    """Select the most appropriate fix from the fixed vocabulary."""
    if kill_switch_fired:
        return "Implement Circuit Breaker"
    mapping = {
        "container_crash": "Increase Replicas",
        "network_latency": "Adjust Timeouts",
        "disk_fill": "Add Retries",
    }
    return mapping.get(fault_type, "Add Retries")
