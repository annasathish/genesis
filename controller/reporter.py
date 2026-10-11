"""
M5 - Report Generator & Aggregator

Reads directly from the SQLite database (no FastAPI dependency).
Produces report.json and REPORT.md.

Key design:
  - All metrics are extracted from the DB in Python.
  - Numeric values are injected into the Jinja2 template.
  - The LLM (optional) only fills explanation + fix recommendation.
"""

import json
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from jinja2 import Environment, FileSystemLoader
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import ExperimentIntent, ExperimentResult, MetricsSnapshot
from scorer import compute_resilience_score
from analyzer import analyze_experiment

# ── Constants ───────────────────────────────────────────────────────

DEFAULT_DB_PATH = os.environ.get(
    "CHAOS_DB_PATH", "sqlite:///./chaos_state.db"
)
TEMPLATE_DIR = Path(__file__).parent
TEMPLATE_FILE = "template.md.j2"


# ── Database Access ─────────────────────────────────────────────────

def _get_session(db_url: str = DEFAULT_DB_PATH):
    """Create a standalone SQLAlchemy session (no FastAPI needed)."""
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    try:  # older databases lack columns added to the models later
        from models import ensure_columns
        ensure_columns(engine)
    except Exception:  # read-only or missing DB: the queries below report it
        pass
    Session = sessionmaker(bind=engine)
    return Session()


# ── Metric Helpers ──────────────────────────────────────────────────

def _get_baseline_metrics(session) -> dict:
    """
    Extract baseline p95 and error rate from MetricsSnapshot rows
    where is_baseline == True.
    """
    rows = (
        session.query(MetricsSnapshot)
        .filter(MetricsSnapshot.is_baseline == True)
        .all()
    )
    if not rows:
        return {"p95_latency": 0.0, "error_rate": 0.0}

    p95_values = [r.p95_latency for r in rows]
    err_values = [r.error_rate for r in rows]

    return {
        "p95_latency": statistics.mean(p95_values),
        "error_rate": statistics.mean(err_values),
    }


def _get_during_fault_metrics(
    session, intent: ExperimentIntent
) -> dict:
    """
    Get average metrics captured DURING the experiment window
    (non-baseline snapshots after the experiment was created).
    """
    rows = (
        session.query(MetricsSnapshot)
        .filter(
            MetricsSnapshot.is_baseline == False,
            MetricsSnapshot.timestamp >= intent.created_at,
        )
        .all()
    )
    if not rows:
        # Fallback: use baseline values (experiment may not have run yet)
        return {"p95_latency": 0.0, "error_rate": 0.0}

    return {
        "p95_latency": statistics.mean([r.p95_latency for r in rows]),
        "error_rate": statistics.mean([r.error_rate for r in rows]),
    }


# ── Criticality Classification ─────────────────────────────────────

def _classify_criticality(score_10: float) -> str:
    """Map a /10 score to a human-readable criticality label."""
    if score_10 <= 3.0:
        return "Critical"
    if score_10 <= 5.0:
        return "High"
    if score_10 <= 7.0:
        return "Medium"
    return "Low"


# ── Core Report Builder ────────────────────────────────────────────

def generate_report(
    db_url: str = DEFAULT_DB_PATH,
    use_llm: bool = False,
    output_dir: str = ".",
) -> dict:
    """
    Main entry point.  Reads the SQLite DB, scores every experiment,
    optionally calls the LLM for analysis, and writes report.json + REPORT.md.

    Returns the full report data dict.
    """
    session = _get_session(db_url)

    try:
        baseline = _get_baseline_metrics(session)
        intents = session.query(ExperimentIntent).all()
        results = {
            r.intent_id: r
            for r in session.query(ExperimentResult).all()
        }

        experiments: List[dict] = []
        weaknesses: List[dict] = []

        for intent in intents:
            result = results.get(intent.id)
            if result is None:
                continue  # skip intents that never produced a result

            during = _get_during_fault_metrics(session, intent)

            # Compute normalised score
            score = compute_resilience_score(
                p95_during=during["p95_latency"],
                p95_baseline=baseline["p95_latency"],
                error_rate=during["error_rate"],
                recovery_time_sec=result.recovery_time_sec,
                kill_switch_fired=result.kill_switch_fired,
            )

            # Optional LLM analysis
            analysis = analyze_experiment(
                fault_type=intent.fault_type,
                target_service=intent.target_service,
                fault_params=intent.fault_params or "{}",
                p95_baseline=baseline["p95_latency"],
                p95_during=during["p95_latency"],
                error_rate=during["error_rate"],
                recovery_time_sec=result.recovery_time_sec,
                kill_switch_fired=result.kill_switch_fired,
                score_out_of_10=score["score_out_of_10"],
                use_llm=use_llm,
            )

            exp_data = {
                "intent_id": intent.id,
                "fault_type": intent.fault_type,
                "target_service": intent.target_service,
                "fault_params": intent.fault_params or "{}",
                "status": intent.status,
                "p95_baseline": round(baseline["p95_latency"], 2),
                "p95_during": round(during["p95_latency"], 2),
                "error_rate_pct": round(during["error_rate"] * 100, 2),
                "recovery_time": (
                    round(result.recovery_time_sec, 2)
                    if result.recovery_time_sec is not None
                    else "N/A"
                ),
                "kill_switch_fired": result.kill_switch_fired,
                **score,
                "llm_analysis": analysis.get("explanation"),
                "analysis": analysis.get("explanation"),
                "recommended_fix": analysis.get("recommended_fix"),
            }
            experiments.append(exp_data)

            weaknesses.append({
                "service": intent.target_service,
                "fault_type": intent.fault_type,
                "score": score["score_out_of_10"],
                "criticality": _classify_criticality(score["score_out_of_10"]),
                "recommended_fix": analysis.get("recommended_fix"),
                "analysis": analysis.get("explanation"),
            })

        # Sort weaknesses by score ascending (worst first)
        weaknesses.sort(key=lambda w: w["score"])

        # Aggregate
        scores = [e["composite_score"] for e in experiments]
        overall = round(statistics.mean(scores) * 10, 2) if scores else 0.0
        kill_switch_count = sum(1 for e in experiments if e["kill_switch_fired"])
        kill_switch_services = list({
            e["target_service"]
            for e in experiments
            if e["kill_switch_fired"]
        })
        weakest = weaknesses[0]["service"] if weaknesses else "N/A"

        report_data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_experiments": len(experiments),
            "overall_score": overall,
            "kill_switch_count": kill_switch_count,
            "kill_switch_services": kill_switch_services,
            "weakest_service": weakest,
            "experiments": experiments,
            "weaknesses": weaknesses,
        }

        # ── Write Outputs ───────────────────────────────────────────
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        # report.json
        json_path = out / "report.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2, default=str)

        # REPORT.md via Jinja2
        env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            keep_trailing_newline=True,
        )
        template = env.get_template(TEMPLATE_FILE)
        md_content = template.render(**report_data)

        md_path = out / "REPORT.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)

        print(f"[M5] report.json -> {json_path.resolve()}")
        print(f"[M5] REPORT.md   -> {md_path.resolve()}")

        return report_data

    finally:
        session.close()
