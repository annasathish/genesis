"""Interactive resilience dashboard + Copilot chat (host process).

    python -m insights.dashboard.server            # from controller/  (or: make dashboard)
    -> http://127.0.0.1:8050

Separate from the controller API on :8000 on purpose: it needs no Docker restart, no DB lock, and only binds
to localhost. Experiment data comes from runs/*.json; live data from Locust, the controller, Toxiproxy and
`docker ps` (insights/live.py). PII masking runs on everything internally; it is not a UI feature.
"""
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from insights import llm
from insights.business import business_summary
from insights.chat import Copilot, run_brief
from insights.compare import compare_runs
from insights.live import LiveMonitor
from insights.pii import APPLIED_AT, POLICY, Masker
from insights.runs import DEFAULT_RUNS_DIR, load_runs
from insights.vulnerability import scoreboard

STATIC = Path(__file__).with_name("static")


def build_facts(runs_dir: Path = None, assumptions: Dict[str, Any] = None, live: Dict[str, Any] = None) -> Dict[str, Any]:
    masker = Masker()
    runs = load_runs(runs_dir or DEFAULT_RUNS_DIR, masker)
    comp = compare_runs(runs)
    return {
        "runs": [{"run_id": r["run_id"], "label": r["label"], "window_s": r["window_s"],
                  "baseline": {k: r["baseline"][k] for k in ("error_rate", "p95_ms", "threshold")},
                  "windows": [{k: w[k] for k in ("t", "phase", "req", "fail", "err", "p95", "failing", "down")}
                              for w in r["windows"]]} for r in runs],
        "metrics": comp["metrics"],
        "groups": comp["groups"],
        "method": comp["method"],
        "business": business_summary(comp["metrics"], assumptions),
        "vulnerability": scoreboard(comp["metrics"], live),
        # internal only: used by the Copilot to answer privacy questions, stripped from UI responses
        "pii": {**masker.report(), "policy": POLICY, "applied_at": APPLIED_AT},
    }


class ChatIn(BaseModel):
    question: str = Field(..., max_length=2000)
    history: List[Dict[str, str]] = []
    assumptions: Optional[Dict[str, Any]] = None


def create_app(runs_dir: Path = None, api_key: str = None, monitor: LiveMonitor = None) -> FastAPI:
    app = FastAPI(title="Genesis Resilience Dashboard", docs_url="/api/docs")
    mon = monitor or LiveMonitor()

    @app.middleware("http")
    async def no_stale_ui(request, call_next):
        # always revalidate the page and its JS/CSS, so a `git pull` shows up on a plain refresh
        resp = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp
    key = (lambda: api_key) if api_key is not None else llm.api_key  # re-read controller/.env every call

    def ui_facts(assumptions=None):
        facts = build_facts(runs_dir, assumptions, mon.sample())
        facts.pop("pii", None)
        return facts

    @app.get("/api/health")
    def health():
        return {"status": "ok", "llm": bool(key()), "model": llm.configured_model() if key() else None}

    @app.get("/api/overview")
    def overview():
        return ui_facts()

    @app.get("/api/live")
    def live():
        snap = mon.sample()
        facts = build_facts(runs_dir, live=snap)
        return {**snap, "vulnerability": facts["vulnerability"]}

    @app.post("/api/business")
    def business(assumptions: Dict[str, Any]):
        return ui_facts(assumptions)["business"]

    @app.post("/api/brief/{run_id}")
    def brief(run_id: str, assumptions: Optional[Dict[str, Any]] = None):
        facts = build_facts(runs_dir, assumptions)
        m = next((x for x in facts["metrics"] if x["run_id"] == run_id), None)
        b = next((x for x in facts["business"]["runs"] if x["run_id"] == run_id), None)
        if not m or not b:
            raise HTTPException(404, "run not found")
        return run_brief(m, b, facts["business"]["assumptions"], key=key())

    @app.post("/api/chat")
    def chat(body: ChatIn):
        facts = build_facts(runs_dir, body.assumptions, mon.sample())
        facts.pop("runs", None)  # raw windows are noise for the assistant; metrics carry the numbers
        live_snap = mon.sample()
        facts["live"] = {k: live_snap[k] for k in ("status", "window", "kill_switch", "active_faults", "active_toxics",
                                                   "sources")}
        facts["live"]["services"] = {s: {"failing": v["failing"], "live_error_rate": v["live_error_rate"]}
                                     for s, v in live_snap["services"].items()}
        return Copilot(facts, api_key=key()).ask(body.question, body.history)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DASHBOARD_PORT", "8050")))
