"""Multi-agent pipeline: Planner, Analyzer, Recommender, Coordinator, grounding guard, report endpoints."""
import importlib.util
import json
from pathlib import Path

import pytest

from agents import base, coordinator, planner
from agents.analyzer import AnalyzerAgent, severity
from agents.planner import PlannerAgent, score_all, select
from agents.recommender import FIXES, RecommenderAgent
from insights import compare, llm, metrics, runs
from tests.test_insights import make_run

REPO = Path(__file__).resolve().parents[2]


def _facts(rs):
    c = compare.compare_runs(rs)
    from insights.business import business_summary
    from insights.vulnerability import scoreboard
    return {"metrics": c["metrics"], "groups": c["groups"], "method": c["method"],
            "business": business_summary(c["metrics"]), "vulnerability": scoreboard(c["metrics"]), "edges": []}


def _fake_llm(monkeypatch, payload):
    def gen(system, contents, key=None, max_tokens=700, temperature=0.2, json_mode=False, **kw):
        assert json_mode, "agents must ask for JSON"
        return {"text": json.dumps(payload), "model": "gemini-test"}
    monkeypatch.setattr(llm, "generate", gen)


# ---------------------------------------------------------------------------------------------- planner
def test_planner_targets_match_m3_injectors():
    from faults.disk_stress import DiskStressFault
    from faults.manager import ChaosManager
    from faults.rabbitmq_block import RabbitMQFault
    assert set(planner.DISK_TARGETS) == set(DiskStressFault.APPROVED_MOUNTS)
    assert planner.RABBIT_TARGETS == sorted(RabbitMQFault.APPROVED_TARGETS)
    supported = {"container_crash", "network_latency", "disk_fill", "rabbitmq_block"}
    assert {c["fault"] for c in planner.candidates()} <= supported
    import inspect
    assert all(f'"{f}"' in inspect.getsource(ChaosManager.__init__) for f in supported)


def test_planner_proxies_match_toxiproxy_config():
    cfg = json.loads((REPO / "sandbox" / "toxiproxy" / "toxiproxy.json").read_text())
    names = {p["name"] for p in (cfg if isinstance(cfg, list) else cfg.get("proxies", []))}
    assert set(planner.PROXIES) <= names


def test_planner_selects_payment_path_within_budget():
    ms = [metrics.run_metrics(make_run("a", 60))]  # one catalogue crash already run
    ranked = score_all(ms)
    picked = select(ranked, already_run=1)
    assert len(picked) == 3                                   # 4 experiments in total
    assert len({p["fault"] for p in picked}) == 3             # one per fault type
    assert [p["id"] for p in picked] == ["network_latency:payment_to_gateway", "container_crash:payment",
                                         "rabbitmq_block:rabbitmq"]
    assert all(p["reason"] for p in picked)
    assert next(r for r in ranked if r["id"] == "container_crash:catalogue")["already_run"]
    assert select(ranked, already_run=4) == []                # budget used


def test_planner_never_proposes_unsafe_targets():
    ids = {c["id"] for c in planner.candidates()}
    assert "container_crash:web" not in ids and "disk_fill:mysql" not in ids
    assert all(c["target"] in planner.PROXIES for c in planner.candidates() if c["fault"] == "network_latency")


def test_planner_llm_rerank_ignores_unknown_ids_and_invented_numbers(monkeypatch):
    facts = _facts([make_run("a", 60)])
    _fake_llm(monkeypatch, {"ranking": [
        {"id": "rabbitmq_block:rabbitmq", "reason": "Orders queue is a single broker on the payment path."},
        {"id": "container_crash:web; rm -rf /", "reason": "nope"},
        {"id": "container_crash:payment", "reason": "Would lose 4,321 orders per hour."}]})
    out = PlannerAgent(use_llm=True, key="k").run(facts)
    plan, trace = out["output"], out["trace"]
    assert trace["mode"] == "llm" and plan["ranked_by"] == "ai"
    assert plan["ranked"][0]["id"] == "rabbitmq_block:rabbitmq" and plan["ranked"][0]["ai_reason"]
    pay = next(r for r in plan["ranked"] if r["id"] == "container_crash:payment")
    assert "ai_reason" not in pay                                   # 4,321 is not in the data
    assert any("unknown candidate" in n for n in trace["notes"])
    assert any("4,321" in n for n in trace["notes"])
    assert len(plan["selected"]) == 3


# ---------------------------------------------------------------------------------------------- grounding guard
def test_ungrounded_numbers():
    allowed = base.allowed_numbers({"peak": 0.667, "ms": 2000, "cost": 565.48, "text": "p95 33 ms"})
    assert base.ungrounded("peak 66.7% at 2,000 ms, cost 565.48, p95 33 ms", allowed) == []
    assert base.ungrounded("lost $15,000 and 2400ms", allowed) == ["15,000", "2400"]
    assert base.ungrounded("3 services in 2026", allowed) == []        # small counts and years tolerated
    assert base.ungrounded("15000 orders", base.allowed_numbers({"x": 15})) == ["15000"]   # no zero-stripping of integers
    assert base.ungrounded("2.50 s", base.allowed_numbers({"x": 2.5})) == []


# ---------------------------------------------------------------------------------------------- analyzer / recommender
def test_analyzer_ranks_by_severity_and_scores():
    rs = [make_run("a", 20, target="cart"), make_run("b", 90, target="catalogue"),
          make_run("c", 5, fault="network_latency", target="payment_to_gateway")]
    out = AnalyzerAgent(use_llm=False).run(_facts(rs))["output"]
    sev = [f["severity_score"] for f in out["findings"]]
    assert sev == sorted(sev, reverse=True) and out["findings"][0]["target"] == "catalogue"
    assert [f["rank"] for f in out["findings"]] == [1, 2, 3]
    assert out["overall_resilience"] is not None and set(out["failure_types"]) == {"container_crash", "network_latency"}
    lat = next(f for f in out["findings"] if f["fault"] == "network_latency")
    assert lat["service"] == "payment" and lat["edge"]["dependency"] == "gateway"


def test_slowdown_raises_severity():
    m = metrics.run_metrics(make_run("a", 0))
    calm = severity(m)
    base_points = calm["severity_score"] - calm["components"]["slowdown_points"]
    m["slowest_endpoint"] = {"endpoint": "payment:pay", "baseline_ms": 20, "peak_ms": 2100, "ratio": 10.0}
    slow = severity(m)
    assert slow["components"]["slowdown_points"] == 20.0 and slow["p95_ratio"] == 10.0
    assert slow["severity_score"] == pytest.approx(base_points + 20, abs=0.1)


def test_analyzer_rejects_invented_numbers(monkeypatch):
    facts = _facts([make_run("a", 60)])
    sig = facts["metrics"][0]["signature"]
    _fake_llm(monkeypatch, {"findings": [{"id": sig, "explanation": "Catalogue went down; $15,000 lost in 5 minutes."}],
                            "summary": "One experiment; the catalogue is a single point of failure."})
    out = AnalyzerAgent(use_llm=True, key="k").run(facts)
    f = out["output"]["findings"][0]
    assert "15,000" not in f["explanation"] and f.get("explained_by") != "ai"
    assert out["output"]["summary"].startswith("One experiment")
    assert any("15,000" in n for n in out["trace"]["notes"])


def test_recommender_validates_fix_category(monkeypatch):
    facts = _facts([make_run("a", 60), make_run("b", 30, fault="rabbitmq_block", target="rabbitmq")])
    facts["analysis"] = AnalyzerAgent(use_llm=False).run(facts)["output"]
    ids = [f["id"] for f in facts["analysis"]["findings"]]
    _fake_llm(monkeypatch, {"recommendations": [
        {"id": ids[0], "fix": "Rewrite in Rust", "action": "Rewrite everything."},
        {"id": ids[1], "fix": "Implement Circuit Breaker", "action": "Wrap the call in a breaker with a cached fallback."}]})
    out = RecommenderAgent(use_llm=True, key="k").run(facts)
    recs = {r["id"]: r for r in out["output"]["recommendations"]}
    assert recs[ids[0]]["fix"] in FIXES and recs[ids[0]]["fix"] != "Rewrite in Rust"
    assert recs[ids[1]]["fix"] == "Implement Circuit Breaker" and recs[ids[1]]["written_by"] == "ai"
    assert [r["priority"] for r in out["output"]["recommendations"]] == [1, 2]
    assert all("--target" in r["verify"] for r in out["output"]["recommendations"])


def test_llm_failure_keeps_rules(monkeypatch):
    def boom(*a, **k):
        raise llm.LLMError("Gemini rate limit / quota exceeded (HTTP 429)")
    monkeypatch.setattr(llm, "generate", boom)
    out = AnalyzerAgent(use_llm=True, key="k").run(_facts([make_run("a", 60)]))
    assert out["trace"]["mode"] == "rules" and "429" in out["trace"]["notes"][0]
    assert out["output"]["findings"]


# ---------------------------------------------------------------------------------------------- coordinator + endpoints
def _write_runs(tmp_path):
    for i, (fault, target, e) in enumerate([("container_crash", "catalogue", 60), ("network_latency", "payment_to_gateway", 0)]):
        r = make_run(f"x{i}", e, fault=fault, target=target)
        raw = {"target": r["target"], "fault": r["fault"], "windows": r["windows"], "baseline": r["baseline"],
               "summary": r["summary"]}
        (tmp_path / f"day1_20261010_10000{i}.json").write_text(json.dumps(raw))


def test_coordinator_writes_report(tmp_path):
    _write_runs(tmp_path)
    r = coordinator.run_pipeline(tmp_path, use_llm=False, out_dir=tmp_path / "out")
    assert [t["agent"] for t in r["agents"]] == ["planner", "analyzer", "recommender"]
    md = (tmp_path / "out" / "FINDINGS_REPORT.md").read_text(encoding="utf-8")
    for section in ("Executive summary", "Ranked weaknesses", "Recommended fixes", "Next experiments", "agents"):
        assert section in md
    js = json.loads((tmp_path / "out" / "findings_report.json").read_text(encoding="utf-8"))
    assert js["distinct_experiments"] == 2 and len(js["recommendations"]) == 2


def test_report_endpoints(tmp_path):
    from fastapi.testclient import TestClient
    from insights.dashboard.server import create_app
    from tests.test_insights import live

    class Dead(live.LiveMonitor):
        def _locust(self): return {"ok": False, "error": "x"}
        def _intents(self): return {"ok": False, "error": "x", "active": []}
        def _toxics(self): return {"ok": False, "error": "x", "active": []}
        def _safety(self): return {"ok": False, "error": "x"}
        def _probes(self): return {}
    _write_runs(tmp_path)
    c = TestClient(create_app(runs_dir=tmp_path, api_key="", monitor=Dead(docker_cmd=("nope",))))
    assert c.get("/api/report/latest").status_code == 404
    assert c.get("/api/plan").json()["budget"] == planner.MAX_EXPERIMENTS
    r = c.post("/api/report", json={"use_llm": True}).json()
    assert r["agents"][0]["notes"] == ["no GEMINI_API_KEY: rule-based output"]
    assert c.get("/api/report/latest").json()["generated_at"] == r["generated_at"]
    assert c.get("/api/report/download/md").text.startswith("# Findings Report")
    assert c.get("/api/report/download/zip").status_code == 404
    ans = c.post("/api/chat", json={"question": "summarise the findings report"}).json()
    assert "Findings report" in ans["answer"]


# ---------------------------------------------------------------------------------------------- day1_demo helpers
def test_demo_latency_helpers():
    spec = importlib.util.spec_from_file_location("d", REPO / "scripts" / "day1_demo.py")
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    lat = d.latency_deltas({"payment:pay": (10, 200.0)}, {"payment:pay": (12, 4400.0), "web:index": (5, 50.0)})
    assert lat == {"payment:pay": 2100.0, "web:index": 10.0}
    assert d.slow_endpoints(lat, {"payment:pay": 20.0, "web:index": 9.0}, 5.0) == ["payment:pay"]
    assert d.slow_endpoints({"web:index": 150.0}, {"web:index": 5.0}, 5.0) == []   # under 200 ms is not "slow"


# ---------------------------------------------------------------------------------------------- fix verification
def test_fix_phases_pooled_and_verified():
    from insights.compare import BASELINE_PHASE, phase_of
    assert phase_of("") == BASELINE_PHASE and phase_of("planner #1: x") == BASELINE_PHASE
    assert phase_of("fix: 2 replicas") == "fix: 2 replicas"
    rs = [make_run(f"b{i}", e, n_per_window=200) for i, e in enumerate([40, 55, 45])]
    rs += [make_run(f"f{i}", 0, n_per_window=200, label="fix: 2 replicas") for i in range(2)]
    g = compare.compare_runs(rs)["groups"][0]
    assert [p["phase"] for p in g["phases"]] == [BASELINE_PHASE, "fix: 2 replicas"]
    base = g["phases"][0]
    assert base["n_runs"] == 3 and base["fault_requests"] == 3 * 6 * 200 and base["noise_band"]["max"] > base["noise_band"]["min"]
    (eff,) = g["effects"]
    assert eff["verdict"] == "improved" and eff["primary_relative_change"] == pytest.approx(1.0)
    assert [t["phase"] for t in g["trend"]].count("fix: 2 replicas") == 2

    facts = _facts(rs)
    f = AnalyzerAgent(use_llm=False).run(facts)["output"]["findings"][0]
    assert f["latest_run"] == "b2"                               # weakness = system without the fix
    assert f["fix_verification"][0]["verdict"] == "improved" and f["fix_verification"][0]["runs_after"] == 2
    facts["analysis"] = {"findings": [f]}
    rec = RecommenderAgent(use_llm=False).run(facts)["output"]["recommendations"][0]
    assert rec["verified_fix"]["fix"] == "fix: 2 replicas"
    assert '--label "fix: ' in rec["verify"]


def test_no_fix_means_no_effects():
    g = compare.compare_runs([make_run("a", 30), make_run("b", 35)])["groups"][0]
    assert g["effects"] == [] and len(g["phases"]) == 1


# ---------------------------------------------------------------------------------------------- experiment console
def test_experiment_console_safety(tmp_path, monkeypatch):
    import sys
    import time as _t
    from fastapi.testclient import TestClient
    from insights.dashboard import control
    from insights.dashboard.server import create_app
    from tests.test_insights import live

    echo = tmp_path / "echo_demo.py"
    echo.write_text("import sys, time\nprint('ARGS', sys.argv[1:])\ntime.sleep(0.6)\n")
    monkeypatch.setattr(control, "DEMO", echo)

    class Dead(live.LiveMonitor):
        def _locust(self): return {"ok": False, "error": "x"}
        def _intents(self): return {"ok": False, "error": "x", "active": []}
        def _toxics(self): return {"ok": False, "error": "x", "active": []}
        def _safety(self): return {"ok": False, "error": "x"}
        def _probes(self): return {}

    runner = control.ExperimentRunner("http://127.0.0.1:9")
    gate = {"available": True, "is_safe_to_inject": True, "reason": None, "kill_switch_state": "IDLE"}
    monkeypatch.setattr(runner, "safety", lambda: dict(gate))
    _write_runs(tmp_path)
    c = TestClient(create_app(runs_dir=tmp_path, api_key="", monitor=Dead(docker_cmd=("nope",)), runner=runner))

    st = c.get("/api/control/status").json()
    assert st["menu"] and st["recommended"] and st["job"] is None
    assert c.post("/api/control/run", json={"candidate_id": "rabbitmq_block:rabbitmq"}).status_code == 400   # no approval
    assert c.post("/api/control/run", json={"candidate_id": "x; rm -rf /", "approve": True}).status_code == 404
    gate["is_safe_to_inject"], gate["reason"] = False, "Capture a baseline first"
    r = c.post("/api/control/run", json={"candidate_id": "rabbitmq_block:rabbitmq", "approve": True})
    assert r.status_code == 409 and "baseline" in r.json()["detail"]
    gate["is_safe_to_inject"] = True

    hostile = 'x"; & del /q * & echo "'
    r = c.post("/api/control/run", json={"candidate_id": "network_latency:payment_to_gateway", "approve": True,
                                         "label": hostile})
    assert r.status_code == 200 and r.json()["job"]["fault"] == "network_latency"
    assert c.post("/api/control/run", json={"candidate_id": "rabbitmq_block:rabbitmq", "approve": True}).status_code == 409
    for _ in range(50):
        if not c.get("/api/control/status").json()["job"]["running"]:
            break
        _t.sleep(0.1)
    out = "\n".join(c.get("/api/control/status").json()["output"])
    assert "'--fault', 'network_latency', '--target', 'payment_to_gateway'" in out
    assert '"latency_ms": 2000' in out and hostile in out          # label passed as one argument, never parsed
    stop = c.post("/api/control/stop").json()
    assert stop["kill_switch_trip"]["ok"] is False and stop["terminated_run"] is False


def test_phase_after_a_fix_is_named_fix_removed():
    from insights.compare import BASELINE_PHASE, REVERTED_PHASE
    rs = [make_run("b1", 40, n_per_window=200), make_run("f1", 0, n_per_window=200, label="fix: replicas"),
          make_run("r1", 45, n_per_window=200, label="dashboard: re-run")]
    g = compare.compare_runs(rs)["groups"][0]
    assert [p["phase"] for p in g["phases"]] == [BASELINE_PHASE, "fix: replicas", REVERTED_PHASE]
    assert [e["verdict"] for e in g["effects"]] == ["improved", "regressed"]


# ---------------------------------------------------------------------------------------------- autopilot campaign
class _FakeRunner:
    """Stands in for ExperimentRunner: each 'experiment' finishes instantly with day1_demo-style output."""
    def __init__(self, safe_after=0):
        self.started, self.calls, self.safe_after, self._job = [], 0, safe_after, None
    def safety(self):
        self.calls += 1
        ok = self.calls > self.safe_after
        return {"available": True, "is_safe_to_inject": ok, "reason": None if ok else "kill switch RECOVERING"}
    def running(self):
        return False
    def start(self, cand, label=""):
        self.started.append((cand["id"], label))
        self._job = {"exit_code": 0, "running": False}
    def status(self):
        return {"job": self._job, "output": ["time to detect   : 10.4 s", "time to recover  : 15.7 s after rollback",
                                             "evidence         : GET /api/x/health via the shop -> timeout at +5.2s",
                                             "recovered        : True"]}
    def stop(self, reason=""):
        return {"kill_switch_trip": {"ok": True}}


def _wait(c, states=("done", "aborted", "failed"), timeout=10):
    import time as _t
    t0 = _t.time()
    while c.state not in states and _t.time() - t0 < timeout:
        _t.sleep(0.05)


def test_campaign_runs_planner_experiments_without_human_choice(monkeypatch):
    from insights.dashboard import control
    monkeypatch.setattr(control, "COOLDOWN_S", 0)
    plan = [{"id": f"container_crash:s{i}", "fault": "container_crash", "target": f"s{i}", "params": {}, "reason": "r"}
            for i in range(4)]
    runner, reports = _FakeRunner(safe_after=2), []
    c = control.Campaign(runner, plan_fn=lambda n: plan[:n], report_fn=lambda: reports.append(1) or
                         {"generated_at": "t", "overall_resilience": 6.0, "agents": [{"mode": "rules"}]})
    monkeypatch.setattr(c, "_sandbox_healthy", lambda: True)
    monkeypatch.setattr(control, "READY_TIMEOUT_S", 5)
    monkeypatch.setattr(control, "READY_POLL_S", 0.01)
    c.start(4)
    _wait(c)
    assert c.state == "done" and reports == [1]
    assert [i for i, _ in runner.started] == [p["id"] for p in plan]           # all four, in planner order
    assert all(lbl.startswith("autopilot:") for _, lbl in runner.started)
    assert all(s["status"] == "done" and s["result"]["time_to_detect"] == "10.4 s" for s in c.steps)
    assert "timeout" in c.steps[0]["result"]["evidence"]
    with pytest.raises(RuntimeError):
        c._thread.join(); c.state = "running"; c.start(4)                     # one campaign at a time


def test_campaign_auto_heals_a_container_that_stayed_down(monkeypatch):
    """dispatch panics when RabbitMQ goes away and has no restart policy: the campaign restarts it instead of waiting
    until the step is skipped."""
    from insights.dashboard import control
    monkeypatch.setattr(control, "COOLDOWN_S", 0)
    monkeypatch.setattr(control, "READY_POLL_S", 0.01)
    monkeypatch.setattr(control, "HEAL_AFTER_S", 0.05)
    plan = [{"id": "container_crash:s1", "fault": "container_crash", "target": "s1", "params": {}}]
    runner = _FakeRunner()
    runner.lines = []
    c = control.Campaign(runner, plan_fn=lambda n: plan[:n])
    down = {"robotshop-dispatch-1"}
    started = []

    def fake_run(cmd, **kw):
        class R: pass
        r = R()
        if cmd[-2:] == ["start", "robotshop-dispatch-1"]:
            started.append(cmd[-1]); down.clear()
        r.stdout = chr(10).join(f"{n}|Exited (2) 1 minute ago" for n in down)
        return r
    monkeypatch.setattr(control.subprocess, "run", fake_run)
    monkeypatch.setattr(c, "_sandbox_healthy", lambda: not down)
    c.start(1)
    _wait(c)
    assert started == ["robotshop-dispatch-1"]
    assert c.steps[0]["status"] == "done" and c.steps[0]["healed"] == ["robotshop-dispatch-1"]
    assert any("auto-heal" in l for l in runner.lines)


def test_campaign_emergency_stop(monkeypatch):
    from insights.dashboard import control
    monkeypatch.setattr(control, "COOLDOWN_S", 0)
    plan = [{"id": f"x:{i}", "fault": "container_crash", "target": f"t{i}", "params": {}} for i in range(4)]
    runner = _FakeRunner(safe_after=10**9)                                    # never safe: stays waiting
    monkeypatch.setattr(control, "READY_POLL_S", 0.01)
    c = control.Campaign(runner, plan_fn=lambda n: plan[:n])
    monkeypatch.setattr(c, "_sandbox_healthy", lambda: True)
    c.start(4)
    out = c.stop()
    _wait(c)
    assert c.state == "aborted" and out["kill_switch_trip"]["ok"] and runner.started == []
    assert all(s["status"] == "aborted" for s in c.steps)


def test_planner_campaign_covers_recorded_experiments():
    ms = [metrics.run_metrics(make_run("a", 60)),
          metrics.run_metrics(make_run("b", 0, fault="network_latency", target="payment_to_gateway"))]
    plan = planner.campaign(ms, 4)
    ids = [c["id"] for c in plan]
    assert len(plan) == 4 and {"container_crash:catalogue", "network_latency:payment_to_gateway"} <= set(ids)
    assert all(c["id"] in {x["id"] for x in planner.candidates()} for c in plan)      # executable only
    assert "never tested" not in next(c for c in plan if c["id"] == "container_crash:catalogue")["reason"]
    assert len(planner.campaign([], 4)) == 4                                          # fresh clone: planner fills it


def test_log_and_campaign_survive_a_dashboard_restart(tmp_path, monkeypatch):
    import time as _t
    from insights.dashboard import control
    echo = tmp_path / "echo_demo.py"
    echo.write_text("print('[1] baseline: 30s of steady load')\nprint('==== RESULT ====')\nprint('recovered        : True')\n")
    monkeypatch.setattr(control, "DEMO", echo)
    logs = tmp_path / "logs"
    r1 = control.ExperimentRunner("http://127.0.0.1:9", log_dir=logs)
    r1.start({"id": "container_crash:catalogue", "fault": "container_crash", "target": "catalogue"})
    for _ in range(100):
        if not r1.running() and r1.status()["job"].get("exit_code") is not None:
            break
        _t.sleep(0.05)
    _t.sleep(0.2)
    r2 = control.ExperimentRunner("http://127.0.0.1:9", log_dir=logs)          # "dashboard restarted"
    st = r2.status()
    assert st["job"]["target"] == "catalogue" and st["job"]["running"] is False and st["job"]["exit_code"] == 0
    assert "recovered        : True" in st["output"]

    c1 = control.Campaign(r2, plan_fn=lambda n: [], log_dir=logs)
    c1.state, c1.steps = "running", [{"n": 1, "id": "x", "status": "running"}]
    c1._save()
    c2 = control.Campaign(r2, plan_fn=lambda n: [], log_dir=logs)            # restart mid-campaign
    assert c2.state == "interrupted" and c2.steps[0]["status"] == "interrupted"
