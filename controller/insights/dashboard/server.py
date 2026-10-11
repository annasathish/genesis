"""Interactive resilience dashboard + Copilot chat (host process).

    python -m insights.dashboard.server            # from controller/  (or: make dashboard)
    -> http://127.0.0.1:8050

Separate from the controller API on :8000 on purpose: it needs no Docker restart, no DB lock, and only binds
to localhost. Experiment data comes from runs/*.json; live data from Locust, the controller, Toxiproxy and
`docker ps` (insights/live.py). PII masking runs on everything internally; it is not a UI feature.
"""
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
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


class ReportIn(BaseModel):
    use_llm: bool = True
    assumptions: Optional[Dict[str, Any]] = None


class RunIn(BaseModel):
    candidate_id: str = Field(..., max_length=120)
    approve: bool = False
    label: str = Field("", max_length=80)


class ChatIn(BaseModel):
    question: str = Field(..., max_length=2000)
    history: List[Dict[str, str]] = []
    assumptions: Optional[Dict[str, Any]] = None


def create_app(runs_dir: Path = None, api_key: str = None, monitor: LiveMonitor = None,
               runner=None, dq_source=None) -> FastAPI:
    app = FastAPI(title="Genesis Resilience Dashboard", docs_url="/api/docs")
    mon = monitor or LiveMonitor()

    @app.middleware("http")
    async def no_stale_ui(request, call_next):
        # always revalidate the page and its JS/CSS, so a `git pull` shows up on a plain refresh
        resp = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp
    key = (lambda: api_key) if api_key is not None else llm.api_key  # re-read the .env files every call

    def ui_facts(assumptions=None):
        facts = build_facts(runs_dir, assumptions, mon.sample())
        facts.pop("pii", None)
        return facts

    @app.get("/api/health")
    def health():
        return {"status": "ok", "llm": bool(key()), "model": llm.configured_model() if key() else None,
                "usage": llm.usage()}

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
    def brief(run_id: str, assumptions: Optional[Dict[str, Any]] = None, ai: bool = True):
        facts = build_facts(runs_dir, assumptions)
        m = next((x for x in facts["metrics"] if x["run_id"] == run_id), None)
        b = next((x for x in facts["business"]["runs"] if x["run_id"] == run_id), None)
        if not m or not b:
            raise HTTPException(404, "run not found")
        if not ai:  # opening the tab: instant rule-based brief, no Gemini call (Generate asks the AI)
            from insights.chat import offline_brief
            return {"brief": offline_brief(m, b, facts["business"]["assumptions"]), "mode": "offline", "note": None}
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
        rep_file = reports_dir / "findings_report.json"
        if rep_file.is_file():
            try:
                r = json.loads(rep_file.read_text(encoding="utf-8"))
                facts["findings_report"] = {k: r.get(k) for k in ("generated_at", "overall_resilience", "summary",
                                                                  "findings", "recommendations")}
                facts["next_experiments"] = [{k: c.get(k) for k in ("id", "score", "reason")}
                                             for c in (r.get("plan") or {}).get("selected", [])]
            except ValueError:
                pass
        return Copilot(facts, api_key=key()).ask(body.question, body.history)

    # ---------------------------------------------------------------- multi-agent findings report
    from agents.coordinator import REPORTS_DIR, build_facts as agent_facts, run_pipeline
    from agents.planner import PlannerAgent
    reports_dir = Path(os.environ.get("REPORTS_DIR", REPORTS_DIR)) if runs_dir is None else Path(runs_dir) / "_reports"
    report_lock = threading.Lock()

    @app.post("/api/report")
    def generate_report(body: ReportIn):
        if not report_lock.acquire(blocking=False):
            raise HTTPException(409, "a report is already being generated")
        try:
            r = run_pipeline(runs_dir, use_llm=body.use_llm, key=key(), assumptions=body.assumptions,
                             out_dir=reports_dir)
        finally:
            report_lock.release()
        r.pop("files", None)
        return r

    @app.get("/api/report/latest")
    def latest_report():
        f = reports_dir / "findings_report.json"
        if not f.is_file():
            raise HTTPException(404, "no report generated yet")
        return json.loads(f.read_text(encoding="utf-8"))

    @app.get("/api/report/download/{kind}")
    def download_report(kind: str):
        name = {"md": "FINDINGS_REPORT.md", "json": "findings_report.json"}.get(kind)
        if not name or not (reports_dir / name).is_file():
            raise HTTPException(404, "no report generated yet")
        return FileResponse(reports_dir / name, filename=name)

    @app.get("/api/plan")
    def plan():
        out = PlannerAgent(use_llm=False).run(agent_facts(runs_dir))  # instant, rule-based preview
        return out["output"]

    # ---------------------------------------------------------------- human-approved experiment runner
    from agents.planner import candidates as executable_candidates
    from insights.dashboard.control import LOG_DIR, ExperimentRunner
    from insights.live import CONTROLLER_URL
    log_dir = LOG_DIR if runs_dir is None else Path(runs_dir) / "logs"
    run_ctl = runner or ExperimentRunner(CONTROLLER_URL, log_dir=log_dir)

    @app.get("/api/control/status")
    def control_status():
        plan = PlannerAgent(use_llm=False).run(agent_facts(runs_dir))["output"]
        pick = lambda c: {k: c[k] for k in ("id", "fault", "target", "params", "score", "reason", "already_run")}
        rerun = [pick(c) for c in plan["ranked"] if c["already_run"]]        # always offered: re-run after a fix
        new = [pick(c) for c in plan["ranked"] if not c["already_run"]][:8]
        budget_left = plan["budget"] - plan["already_run"]
        return {"safety": run_ctl.safety(), "recommended": [c["id"] for c in plan["selected"]],
                "budget": {"max": plan["budget"], "used": plan["already_run"], "left": max(0, budget_left)},
                "menu": rerun + new, **run_ctl.status()}

    @app.post("/api/control/run")
    def control_run(body: RunIn):
        if not body.approve:
            raise HTTPException(400, "explicit approval is required")
        cand = next((c for c in executable_candidates() if c["id"] == body.candidate_id), None)
        if cand is None:
            raise HTTPException(404, "not an executable planner candidate")
        safety = run_ctl.safety()
        if safety["available"] and not safety["is_safe_to_inject"]:
            raise HTTPException(409, f"controller safety gate: {safety['reason']}")
        try:
            return run_ctl.start(cand, body.label)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/control/stop")
    def control_stop():
        if autopilot.state in ("running", "reporting"):
            return autopilot.stop()
        return run_ctl.stop()

    # ---------------------------------------------------------------- autopilot: planner picks, nobody chooses
    from agents.planner import campaign as plan_campaign
    from insights.dashboard.control import Campaign
    autopilot = Campaign(
        run_ctl,
        plan_fn=lambda size: plan_campaign(agent_facts(runs_dir)["metrics"], size),
        report_fn=lambda: run_pipeline(runs_dir, use_llm=True, key=key(), out_dir=reports_dir),
        log_dir=log_dir if runner is None else None,
    )

    @app.get("/api/autopilot/status")
    def autopilot_status():
        st = autopilot.status()
        if st["state"] == "idle":   # preview what Start would run
            st["preview"] = [{k: c[k] for k in ("id", "fault", "target", "params", "reason")}
                             for c in plan_campaign(agent_facts(runs_dir)["metrics"], 4)]
        return st

    @app.post("/api/autopilot/start")
    def autopilot_start():
        safety = run_ctl.safety()
        if safety["available"] and not safety["is_safe_to_inject"]:
            raise HTTPException(409, f"controller safety gate: {safety['reason']}")
        try:
            return autopilot.start(4)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/autopilot/stop")
    def autopilot_stop():
        return autopilot.stop()

    # ---------------------------------------------------------------- data quality (customer-entered data)
    from insights import dataquality as dq
    from insights.dataquality_shop import DataSourceError, ShopData
    dq_dir = Path(runs_dir or DEFAULT_RUNS_DIR) / "dataquality"
    dq_src = dq_source or ShopData(backup_dir=dq_dir)
    dq_lock = threading.Lock()

    def dq_save(doc):
        try:
            dq_dir.mkdir(parents=True, exist_ok=True)
            (dq_dir / "last.json").write_text(json.dumps(doc, default=str), encoding="utf-8")
        except OSError:
            pass

    def dq_scan():
        snap = dq_src.snapshot()
        return snap, dq.analyze(snap["customers"], snap["orders"], snap["catalogue"])

    def dq_guard(fn):
        if not dq_lock.acquire(blocking=False):
            raise HTTPException(409, "a data-quality action is already running")
        try:
            return fn()
        except DataSourceError as exc:
            raise HTTPException(503, str(exc))
        finally:
            dq_lock.release()

    @app.get("/api/data/last")
    def dq_last():
        try:
            return json.loads((dq_dir / "last.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @app.post("/api/data/scan")
    def dq_scan_ep():
        def run():
            snap, a = dq_scan()
            doc = {"stage": "scanned", "before": dq.for_screen(a), "scanned_at": snap["taken_at"]}
            dq_save(doc)
            return doc
        return dq_guard(run)

    @app.post("/api/data/inject")
    def dq_inject():
        def run():
            injected = dq_src.inject_demo()
            snap, a = dq_scan()
            doc = {"stage": "scanned", "injected": injected, "before": dq.for_screen(a), "scanned_at": snap["taken_at"]}
            dq_save(doc)
            return doc
        return dq_guard(run)

    @app.post("/api/data/fix")
    def dq_fix():
        def run():
            snap, before = dq_scan()                       # always fix from a fresh snapshot, never client data
            ops = dq.plan_writes(snap["customers"], snap["orders"], before)
            res = dq_src.apply(ops, snap)
            snap2, after = dq_scan()
            doc = {"stage": "fixed", "before": dq.for_screen(before), "after": dq.for_screen(after),
                   "residual": dq.residual_summary(after), "writes": res["applied"],
                   "backup": Path(res["backup"]).name if res.get("backup") else None,
                   "scanned_at": snap["taken_at"], "fixed_at": snap2["taken_at"]}
            dq_save(doc)
            return doc
        return dq_guard(run)

    @app.post("/api/data/reset")
    def dq_reset():
        def run():
            out = dq_src.reset_demo()
            dq_save({})
            return {"removed": out}
        return dq_guard(run)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DASHBOARD_PORT", "8050")))
