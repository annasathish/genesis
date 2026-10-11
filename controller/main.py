"""FastAPI entry point for the sandboxed chaos engineering controller."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, ExperimentIntent, ExperimentResult, ensure_columns


CURRENT_DIR = Path(__file__).resolve().parent
DATABASE_URL = (
    os.environ.get("CHAOS_DB_URL")
    or os.environ.get("DB_PATH")
    or "sqlite:///./chaos_state.db"
)
if not DATABASE_URL.startswith("sqlite:"):
    raise ValueError("CHAOS_DB_URL must be a SQLite SQLAlchemy URL")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)
ensure_columns(engine)  # older chaos_state.db files lack columns added later (rmq_depth, target_service, details)

BASELINE_PATH = Path(os.environ.get("BASELINE_PATH", CURRENT_DIR / "baseline.json"))
GRAPH_PATH = Path(os.environ.get("GRAPH_PATH", CURRENT_DIR / "graph.json"))
MAX_TELEMETRY_AGE_SECONDS = 15.0
SANDBOX_COMPOSE_PATH = Path(
    os.environ.get("SANDBOX_COMPOSE_PATH", CURRENT_DIR.parent / "sandbox" / "docker-compose.yaml")
)


class FaultRequest(BaseModel):
    fault_type: str
    target: str
    params: Dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: int = Field(default=60, ge=1, le=300)


class CombinedFaultRequest(BaseModel):
    faults: List[FaultRequest] = Field(min_length=1, max_length=5)
    ttl_seconds: int = Field(default=60, ge=1, le=300)


async def _rollback_intents(intent_ids: List[int]) -> List[Dict[str, Any]]:
    """Use each injector's own rollback procedure before the kill switch fallback."""
    from faults.manager import ChaosManager

    manager = ChaosManager(session_factory=SessionLocal)
    results: List[Dict[str, Any]] = []
    for intent_id in intent_ids:
        try:
            results.append(await manager.rollback(intent_id))
        except Exception as exc:
            results.append({"intent_id": intent_id, "status": "error", "error": str(exc)})
    return results


from heartbeat import HeartbeatManager
from kill_switch import KillSwitch

heartbeat_manager = HeartbeatManager()
kill_switch = KillSwitch(
    baseline_path=str(BASELINE_PATH),
    session_factory=SessionLocal,
    rollback_callback=_rollback_intents,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    heartbeat_manager.start_background()
    kill_switch.start_background()
    try:
        yield
    finally:
        heartbeat_manager.stop()
        kill_switch.stop()
        tasks = [heartbeat_manager._task, kill_switch._task]
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)


app = FastAPI(title="AI Chaos Engineering Controller", lifespan=lifespan)

# The team dashboard is controller/insights/dashboard (make dashboard -> http://127.0.0.1:8050). Registered before
# dashboard_api's router so /dashboard lands there; dashboard_api's JSON routes stay available.
from fastapi.responses import RedirectResponse

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8050/")


@app.get("/dashboard", include_in_schema=False)
def dashboard_redirect():
    return RedirectResponse(DASHBOARD_URL)


from dashboard_api import router as dashboard_router

app.include_router(dashboard_router)

baseline_capture_task: Optional[asyncio.Task] = None
baseline_capture_status: Dict[str, Any] = {"state": "idle", "error": None}
injection_lock = asyncio.Lock()


def _active_intents() -> List[ExperimentIntent]:
    db = SessionLocal()
    try:
        return (
            db.query(ExperimentIntent)
            .filter(ExperimentIntent.status.in_(["pending", "running"]))
            .order_by(ExperimentIntent.id)
            .all()
        )
    finally:
        db.close()


def _safety_payload() -> Dict[str, Any]:
    baseline_loaded = kill_switch.load_baseline() is not None
    snapshot = kill_switch.last_snapshot
    state = kill_switch.state.value
    max_error, max_latency = kill_switch.get_thresholds()
    telemetry_age = None
    if snapshot is not None:
        telemetry_age = max(0.0, (datetime.now(timezone.utc) - snapshot.timestamp).total_seconds())
    telemetry_ready = bool(
        snapshot
        and snapshot.locust.available
        and snapshot.docker.available
        and telemetry_age is not None
        and telemetry_age <= MAX_TELEMETRY_AGE_SECONDS
    )
    active_intents = _active_intents()
    safe_state = state in {"IDLE", "RECOVERED"}
    is_safe = baseline_loaded and telemetry_ready and safe_state and not active_intents
    return {
        "baseline_loaded": baseline_loaded,
        "baseline_capture": dict(baseline_capture_status),
        "telemetry_age_seconds": round(telemetry_age, 1) if telemetry_age is not None else None,
        "kill_switch_state": state,
        "kill_switch_reason": kill_switch.last_trip_reason,
        "is_safe_to_inject": is_safe,
        "safety_reason": None if is_safe else _unsafe_reason(
            baseline_loaded, telemetry_ready, state, bool(active_intents)
        ),
        "thresholds": {
            "max_error_rate": max_error,
            "max_p95_latency_ms": max_latency,
        },
        "active_intents": [
            {
                "intent_id": intent.id,
                "fault_type": intent.fault_type,
                "target": intent.target_service,
                "status": intent.status,
            }
            for intent in active_intents
        ],
    }


def _unsafe_reason(
    baseline_loaded: bool,
    telemetry_ready: bool,
    state: str,
    has_active_intents: bool = False,
) -> str:
    if not baseline_loaded:
        return "Capture a steady-state baseline before injecting a fault."
    if not telemetry_ready:
        return "Fresh Locust and Docker telemetry (no older than 15 seconds) is required before injection."
    if has_active_intents:
        return "Wait for the active experiment to roll back before starting another; use the combined experiment endpoint for a reviewed pair."
    return f"Kill switch state is {state}; wait for recovery before injecting another fault."


def _require_safe_to_inject() -> None:
    status = _safety_payload()
    if not status["is_safe_to_inject"]:
        raise HTTPException(status_code=409, detail=status)


@app.get("/")
def read_root():
    return {"status": "Controller is running", "roles": ["M2", "M3", "M4", "M5"]}


@app.post("/api/v1/map")
def build_map():
    import mapper

    if not SANDBOX_COMPOSE_PATH.exists():
        raise HTTPException(status_code=404, detail=f"Sandbox compose file not found: {SANDBOX_COMPOSE_PATH}")
    graph = mapper.generate_dependency_graph(str(SANDBOX_COMPOSE_PATH))
    GRAPH_PATH.write_text(json.dumps(graph, indent=2), encoding="utf-8")
    return {"message": "Graph generated", "graph": graph}


@app.post("/api/v1/plan")
def generate_candidates():
    if not GRAPH_PATH.exists():
        raise HTTPException(status_code=409, detail="Run /api/v1/map before planning experiments.")

    import planner

    graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    candidates = planner.generate_heuristic_candidates(graph)
    return {"status": "success", "candidates": planner.llm_rerank(candidates)}


@app.post("/api/v1/plan/adaptive")
def generate_adaptive_plan(last_intent_id: int):
    if not GRAPH_PATH.exists():
        raise HTTPException(status_code=409, detail="Run /api/v1/map before planning experiments.")

    db = SessionLocal()
    try:
        intent = db.query(ExperimentIntent).filter(ExperimentIntent.id == last_intent_id).first()
        result = db.query(ExperimentResult).filter(ExperimentResult.intent_id == last_intent_id).first()
        if not intent or not result:
            raise HTTPException(status_code=404, detail="Previous intent or result not found.")

        import planner

        candidate = planner.generate_adaptive_candidate(
            {column.name: getattr(intent, column.name) for column in intent.__table__.columns},
            {column.name: getattr(result, column.name) for column in result.__table__.columns},
            json.loads(GRAPH_PATH.read_text(encoding="utf-8")),
        )
        return {"status": "success", "candidate": candidate}
    finally:
        db.close()


@app.post("/api/v1/inject")
async def inject_fault(req: FaultRequest):
    async with injection_lock:
        _require_safe_to_inject()
        from faults.manager import ChaosManager

        manager = ChaosManager(session_factory=SessionLocal)
        result = await manager.execute_fault(
            fault_type=req.fault_type,
            target=req.target,
            params=req.params,
            ttl_seconds=req.ttl_seconds,
        )
        return {"status": "success", "result": result}


@app.post("/api/v1/inject/combined")
async def inject_combined_faults(req: CombinedFaultRequest):
    async with injection_lock:
        _require_safe_to_inject()
        targets = [fault.target for fault in req.faults]
        if len(set(targets)) != len(targets):
            raise HTTPException(status_code=422, detail="Combined faults must target distinct services.")

        from faults.manager import ChaosManager

        manager = ChaosManager(session_factory=SessionLocal)
        results = await asyncio.gather(
            *(
                manager.execute_fault(
                    fault_type=fault.fault_type,
                    target=fault.target,
                    params=fault.params,
                    ttl_seconds=min(fault.ttl_seconds, req.ttl_seconds),
                )
                for fault in req.faults
            ),
            return_exceptions=True,
        )
        formatted = [
            {"status": "error", "message": str(result)}
            if isinstance(result, Exception)
            else {"status": "success", "result": result}
            for result in results
        ]
        status = "success" if all(item["status"] == "success" for item in formatted) else "partial_failure"
        return {"status": status, "results": formatted}


@app.get("/api/v1/intents/active")
def active_intents():
    """Pending/running experiment intents (read-only; used by the dashboard's Live tab)."""
    return [
        {
            "intent_id": intent.id,
            "fault_type": intent.fault_type,
            "target": intent.target_service,
            "status": intent.status,
            "created_at": intent.created_at.isoformat() if intent.created_at else None,
            "expires_at": intent.expires_at.isoformat() if intent.expires_at else None,
        }
        for intent in _active_intents()
    ]


@app.post("/api/v1/rollback/{intent_id}")
async def rollback_fault(intent_id: int):
    from faults.manager import ChaosManager

    result = await ChaosManager(session_factory=SessionLocal).rollback(intent_id)
    return {"status": "success", "result": result}


def _capture_baseline_sync(duration: int, interval: int) -> Dict[str, Any]:
    from baseline import BaselineCollector

    collector = BaselineCollector(
        duration_seconds=duration,
        interval_seconds=interval,
        locust_url=os.environ.get("LOCUST_URL", "http://localhost:8089"),
        output_file=str(BASELINE_PATH),
        db_url=DATABASE_URL,
    )
    return asyncio.run(collector.run())


async def _run_baseline_capture(duration: int, interval: int) -> None:
    global baseline_capture_status
    baseline_capture_status = {"state": "running", "error": None, "duration_seconds": duration}
    try:
        result = await asyncio.to_thread(_capture_baseline_sync, duration, interval)
        kill_switch.load_baseline()
        baseline_capture_status = {
            "state": "complete",
            "error": None,
            "duration_seconds": duration,
            "sample_count": result.get("sample_count", 0),
            "captured_at": result.get("captured_at"),
        }
    except Exception as exc:
        baseline_capture_status = {"state": "failed", "error": str(exc), "duration_seconds": duration}


@app.post("/api/v1/baseline")
async def capture_baseline(
    test_mode: bool = False,
    duration: int = Query(default=600, ge=10, le=3600),
    interval: int = Query(default=5, ge=1, le=60),
):
    global baseline_capture_task
    if baseline_capture_task is not None and not baseline_capture_task.done():
        return {"status": "already_running", **baseline_capture_status}

    actual_duration = 10 if test_mode else duration
    actual_interval = min(interval, 2) if test_mode else interval
    baseline_capture_task = asyncio.create_task(_run_baseline_capture(actual_duration, actual_interval))
    return {"status": "started", "duration": actual_duration, "interval": actual_interval}


@app.get("/api/v1/telemetry/current")
async def get_current_telemetry():
    snapshot = kill_switch.last_snapshot
    if snapshot is not None:
        return snapshot.to_dict()
    return {
        "timestamp": None,
        "is_healthy": False,
        "error_rate": None,
        "p95_latency": None,
        "error_message": "Waiting for the first telemetry sample.",
        "locust": {"available": False, "state": "unknown", "error_message": "No sample yet."},
        "docker": {"available": False, "all_running": False, "error_message": "No sample yet."},
    }


@app.get("/api/v1/safety/status")
def get_safety_status():
    return _safety_payload()


@app.get("/api/v1/safety/heartbeat")
def get_heartbeat_status():
    exists, age, payload = heartbeat_manager.read_heartbeat()
    return {
        "heartbeat_active": exists and age <= 20,
        "age_seconds": age if exists else None,
        "heartbeat": payload,
    }


@app.post("/api/v1/safety/trip")
async def trip_safety(reason: str = "Manual Override"):
    result = await kill_switch.manual_trip(reason)
    return {"status": "tripped", "reason": reason, "rollback": result}
