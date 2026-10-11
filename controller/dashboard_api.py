from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from models import ExperimentIntent, ExperimentResult, MetricsSnapshot
import os
import time
from datetime import datetime, timezone

router = APIRouter()

# Dependency to get DB
def get_db():
    from main import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/dashboard", response_class=HTMLResponse)
def get_dashboard():
    path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    with open(path, "r") as f:
        return f.read()

@router.get("/api/v1/metrics")
def get_metrics(db: Session = Depends(get_db)):
    # Fetch latest metrics snapshot
    latest = db.query(MetricsSnapshot).order_by(MetricsSnapshot.timestamp.desc()).first()
    active = (
        db.query(ExperimentIntent)
        .filter(ExperimentIntent.status.in_(["pending", "running"]))
        .order_by(ExperimentIntent.id.desc())
        .first()
    )
    
    error_rate = latest.error_rate if latest else 0.0
    p95 = latest.p95_latency if latest else 0.0
    
    from main import kill_switch

    state = kill_switch.state.value
    kill_switch_open = state in {"WARNING_WINDOW_1", "TRIPPED", "RECOVERING"}
    
    if not latest:
        status_msg = "⚪ WAITING FOR TELEMETRY"
    elif state == "TRIPPED":
        status_msg = "🛑 KILL SWITCH TRIPPED — ROLLBACK STARTED"
    elif state == "RECOVERING":
        status_msg = "🟠 SYSTEM RECOVERING"
    elif active:
        status_msg = f"🔴 INJECTING FAILURE: {active.fault_type} on {active.target_service}"
    elif state == "WARNING_WINDOW_1":
        status_msg = "⚠️ SAFETY THRESHOLD BREACHED — CONFIRMING"
    else:
        status_msg = "🟢 SYSTEM STABLE"
        
    return {
        "error_rate": round(error_rate * 100, 2), # %
        "p95_latency": round(p95, 2),
        "kill_switch_open": kill_switch_open,
        "kill_switch_state": state,
        "status_badge": status_msg,
        "timestamp": latest.timestamp.astimezone(timezone.utc).strftime("%H:%M:%S") if latest and latest.timestamp else datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "rmq_depth": getattr(latest, "rmq_depth", 0) if latest else 0
    }

@router.get("/api/v1/rankings")
def get_rankings(db: Session = Depends(get_db)):
    results = db.query(ExperimentIntent, ExperimentResult).join(
        ExperimentResult, ExperimentIntent.id == ExperimentResult.intent_id
    ).all()
    
    scores = {}
    services = ["cart", "rabbitmq", "shipping", "redis", "mongodb", "payment", "user", "catalogue", "dispatch", "ratings"]
    
    for s in services:
        scores[s] = {"error": 0.0, "recovery": 0.0, "cascade": 0.0, "count": 0}
        
    for intent, res in results:
        t = intent.target_service
        if t not in scores:
            scores[t] = {"error": 0.0, "recovery": 0.0, "cascade": 0.0, "count": 0}
            
        resilience = res.resilience_score if res.resilience_score is not None else 1.0
        spike = (1.0 - resilience) * 100
        rec = min(res.recovery_time_sec or 0.0, 300) / 300 * 100
        
        scores[t]["error"] += spike
        scores[t]["recovery"] += rec
        scores[t]["cascade"] += (spike * 0.5)
        scores[t]["count"] += 1
        
    rankings = []
    for s, data in scores.items():
        c = max(1, data["count"])
        e = data["error"] / c
        r = data["recovery"] / c
        cas = data["cascade"] / c
        
        if data["count"] == 0:
            e, r, cas = 0.0, 0.0, 0.0
                
        hv = (e * 0.4) + (r * 0.3) + (cas * 0.3)
        
        if data["count"] == 0:
            status = "WAITING"
        elif hv > 50:
            status = "CRITICAL"
        elif hv > 20:
            status = "FRAGILE"
        else:
            status = "STABLE"
        
        rankings.append({
            "service": s,
            "score": round(hv, 1),
            "status": status,
            "cascade_risk": ["web", "dispatch"] if s in ["cart", "payment", "rabbitmq"] else []
        })
        
    rankings.sort(key=lambda x: x["score"], reverse=True)
    return rankings[:10]

import json
import logging

# Bound LLM calls while allowing current experiment telemetry to refresh.
LLM_INSIGHT_CACHE = {}
LLM_INSIGHT_CACHE_TTL_SECONDS = 15

@router.get("/api/v1/business-insights")
def get_insights(db: Session = Depends(get_db)):
    intent = db.query(ExperimentIntent).order_by(ExperimentIntent.id.desc()).first()
    latest = db.query(MetricsSnapshot).order_by(MetricsSnapshot.timestamp.desc()).first()
    if not intent:
        return {
            "churn_risk": "UNKNOWN",
            "funnel_impact": "Run a sandbox experiment to connect a fault to an observed user flow.",
            "dau_impact": "Not measured by this sandbox",
            "revenue_at_risk": "Not measured",
            "sla_depletion": "Not measured",
            "support_volume": "Not measured by this sandbox",
            "roi_recommendation": "Capture a baseline, then preview a dependency-aware experiment.",
            "impact_basis": "The sandbox records service telemetry; it does not contain production business data.",
        }

    target = intent.target_service
    fault_type = intent.fault_type
    error_rate = float(latest.error_rate) if latest and latest.error_rate is not None else 0.0
    p95_ms = float(latest.p95_latency) if latest and latest.p95_latency is not None else 0.0
    risk = "HIGH" if error_rate >= 0.05 else "MEDIUM" if error_rate >= 0.01 or p95_ms >= 500 else "LOW"
    observed_revenue_proxy = 100.0 * error_rate
    impact_basis = (
        "Illustrative proxy only: observed request error rate × an assumed $100/min sandbox revenue baseline. "
        "No production revenue, DAU, churn, or support data is available."
    )
    if target in {"cart", "payment"}:
        funnel = "Checkout path is the likely affected flow; verify against the observed error and latency change."
        recommendation = f"Add bounded timeouts and a circuit breaker around {target} calls, then rerun this experiment."
    elif target in {"rabbitmq", "shipping"}:
        funnel = "Order processing may be delayed; inspect queue depth and downstream recovery."
        recommendation = f"Add queue back-pressure and dead-letter handling around {target}, then rerun this experiment."
    else:
        funnel = f"The {target} dependency was targeted; use the dependency map to inspect affected upstream flows."
        recommendation = f"Add a fallback or timeout for callers of {target}, then rerun this experiment."

    insight_data = {
        "churn_risk": risk,
        "funnel_impact": f"{funnel} Current sample: {error_rate:.1%} errors, {p95_ms:.0f} ms p95.",
        "dau_impact": "Not measured by this sandbox",
        "revenue_at_risk": f"~${observed_revenue_proxy:.2f}/min (illustrative)",
        "sla_depletion": "Not measured; compare the experiment window with your SLA objective.",
        "support_volume": "Not measured by this sandbox; use request failures as the available proxy.",
        "roi_recommendation": recommendation,
        "impact_basis": impact_basis,
    }

    cache_key = (intent.id, round(error_rate, 4), round(p95_ms, 1))
    cached = LLM_INSIGHT_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < LLM_INSIGHT_CACHE_TTL_SECONDS:
        return cached[1]

    if os.environ.get("USE_LLM", "false").lower() == "true" and os.environ.get("GEMINI_API_KEY"):
        try:
            from ai_client import generate_json

            prompt = f"""
            You are an SRE reviewing a sandbox-only chaos experiment.
            Target: {target}; fault: {fault_type}.
            Observed error rate: {error_rate:.2%}; observed p95 latency: {p95_ms:.1f} ms.
            Explain one likely technical consequence and recommend one concrete resilience fix.
            Do not invent revenue, DAU, churn, customer counts, or support-ticket figures.
            Return JSON with keys "funnel_impact" and "roi_recommendation" only.
            """
            generated = json.loads(generate_json(prompt))
            if isinstance(generated.get("funnel_impact"), str):
                insight_data["funnel_impact"] = generated["funnel_impact"]
            if isinstance(generated.get("roi_recommendation"), str):
                insight_data["roi_recommendation"] = generated["roi_recommendation"]
        except Exception as exc:
            logger = logging.getLogger(__name__)
            logger.warning("Dashboard AI insight unavailable; using telemetry-grounded guidance: %s", exc)

    LLM_INSIGHT_CACHE[cache_key] = (now, insight_data)
    return insight_data

