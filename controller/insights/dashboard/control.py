"""Human-approved experiment runner for the dashboard (port of the controller dashboard's "Judge Console").

Safety model:
  - only experiments from the Planner's executable candidate list can run; the browser sends a candidate id and
    this module builds the command itself (argument list, no shell), so no browser input reaches a command line
  - an explicit approval flag is required, one experiment at a time
  - the controller's own gate (GET /api/v1/safety/status: baseline, fresh telemetry, kill switch idle, no active
    fault) must allow it when the controller exposes that endpoint
  - the run is scripts/day1_demo.py: measured, recorded in runs/, rolled back automatically, TTL backstop
  - emergency stop trips the controller's kill switch (POST /api/v1/safety/trip) and terminates the run
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional

from insights.runs import REPO_ROOT

DEMO = REPO_ROOT / "scripts" / "day1_demo.py"
LOG_DIR = REPO_ROOT / "runs" / "logs"   # git-ignored (runs/); keeps the last log + campaign across dashboard restarts
TTL_S = 180
MAX_WAIT_S = 120
MAX_LINES = 400


class ExperimentRunner:
    def __init__(self, controller_url: str, runs_dir: Optional[Path] = None, python: str = sys.executable,
                 log_dir: Optional[Path] = None):
        self.controller_url = controller_url.rstrip("/")
        self.runs_dir = runs_dir
        self.python = python
        self._lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.job: Optional[Dict[str, Any]] = None
        self.lines: deque = deque(maxlen=MAX_LINES)
        self.log_dir = Path(log_dir) if log_dir else None
        self._load_last()

    # ------------------------------------------------------------------ persistence (last run survives restarts)
    def _paths(self):
        return self.log_dir / "last_experiment.log", self.log_dir / "last_experiment.json"

    def _load_last(self) -> None:
        if not self.log_dir:
            return
        log, meta = self._paths()
        try:
            self.job = json.loads(meta.read_text(encoding="utf-8"))
            self.job["running"] = False
            self.lines.extend(log.read_text(encoding="utf-8", errors="replace").splitlines()[-MAX_LINES:])
        except (OSError, ValueError):
            self.job = None

    def _save_meta(self) -> None:
        if self.log_dir and self.job:
            try:
                self._paths()[1].write_text(json.dumps(self.job), encoding="utf-8")
            except OSError:
                pass

    # ------------------------------------------------------------------ controller
    def _call(self, method: str, path: str, timeout: float = 5.0) -> Dict[str, Any]:
        req = urllib.request.Request(self.controller_url + path, method=method, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return {"ok": True, "status": r.status, "body": json.loads(r.read().decode() or "{}")}
        except urllib.error.HTTPError as e:
            return {"ok": False, "status": e.code, "body": e.read().decode(errors="replace")[:500]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "status": None, "body": f"{type(e).__name__}: {e}"}

    def safety(self) -> Dict[str, Any]:
        r = self._call("GET", "/api/v1/safety/status")
        if r["ok"] and isinstance(r["body"], dict):
            b = r["body"]
            return {"available": True, "is_safe_to_inject": bool(b.get("is_safe_to_inject")),
                    "reason": b.get("safety_reason"), "kill_switch_state": b.get("kill_switch_state"),
                    "baseline_loaded": b.get("baseline_loaded"), "active_intents": b.get("active_intents", [])}
        return {"available": False, "is_safe_to_inject": None,
                "reason": "controller safety endpoint unavailable; day1_demo.py still checks sandbox health first"}

    # ------------------------------------------------------------------ job
    def status(self) -> Dict[str, Any]:
        job = dict(self.job) if self.job else None
        if job and self.proc is None:
            job.setdefault("running", False)          # loaded from disk after a restart
        if job and self.proc is not None:
            code = self.proc.poll()
            job["running"] = code is None
            job["exit_code"] = code
            job["elapsed_s"] = round(time.time() - job["started"], 1)
        return {"job": job, "output": list(self.lines)[-120:]}

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, candidate: Dict[str, Any], label: str = "") -> Dict[str, Any]:
        with self._lock:
            if self.running():
                raise RuntimeError("an experiment is already running")
            label = (label or f"dashboard: {candidate['id']}").replace("\n", " ")[:80]
            cmd = [self.python, "-u", str(DEMO), "--fault", candidate["fault"], "--target", candidate["target"],
                   "--ttl", str(TTL_S), "--max-wait", str(MAX_WAIT_S), "--label", label]
            if candidate.get("params"):
                cmd += ["--params", json.dumps(candidate["params"])]
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
            self.lines.clear()
            self.proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, encoding="utf-8", errors="replace", env=env)
            self.job = {"candidate": candidate["id"], "fault": candidate["fault"], "target": candidate["target"],
                        "params": candidate.get("params") or {}, "label": label, "started": time.time(),
                        "pid": self.proc.pid}
            if self.log_dir:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                self._paths()[0].write_text("", encoding="utf-8")
                self._save_meta()
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
            return self.status()

    def _pump(self, proc: subprocess.Popen) -> None:
        log = None
        if self.log_dir:
            try:
                log = open(self._paths()[0], "a", encoding="utf-8")
            except OSError:
                log = None
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                self.lines.append(line)
                if log:
                    log.write(line + "\n")
                    log.flush()
        finally:
            if log:
                log.close()
        proc.wait()
        if self.job is not None:
            self.job["exit_code"] = proc.returncode
            self._save_meta()

    def stop(self, reason: str = "DashboardEmergencyStop") -> Dict[str, Any]:
        trip = self._call("POST", f"/api/v1/safety/trip?reason={reason}", timeout=30)
        rolled_back = []
        if not trip["ok"]:  # older controller without the kill-switch endpoint: roll back each active intent
            active = self._call("GET", "/api/v1/intents/active")
            for it in (active["body"] if active["ok"] and isinstance(active["body"], list) else []):
                iid = it.get("intent_id") or it.get("id")
                if iid is not None:
                    rolled_back.append({"intent_id": iid, **self._call("POST", f"/api/v1/rollback/{iid}", timeout=30)})
        terminated = False
        if self.running():
            self.proc.terminate()
            terminated = True
            self.lines.append("[dashboard] emergency stop: kill switch tripped, run terminated")
        return {"kill_switch_trip": trip, "rolled_back": rolled_back, "terminated_run": terminated}


# ---------------------------------------------------------------------------------------------- autonomous campaign
COOLDOWN_S = 20          # steady state between experiments (each run also measures its own 30 s baseline)
READY_TIMEOUT_S = 240    # how long to wait for the safety gate + healthy sandbox before giving up on a step
READY_POLL_S = 3
HEAL_AFTER_S = 30        # waiting this long on an exited sandbox container: restart it instead of skipping the step


class Campaign:
    """Autopilot: the Planner picks the experiments, each is injected, measured and rolled back with no human
    picking, one at a time, waiting for the system to be healthy and the safety gate to allow it in between.
    One Start; Emergency stop aborts. When done, the multi-agent findings report is generated."""

    def __init__(self, runner: ExperimentRunner, plan_fn, report_fn=None, docker_cmd=("docker",),
                 log_dir: Optional[Path] = None):
        self.runner, self.plan_fn, self.report_fn = runner, plan_fn, report_fn
        self.log_dir = Path(log_dir) if log_dir else None
        self.docker_cmd = list(docker_cmd)
        self.state = "idle"
        self.steps: list = []
        self.report: Optional[Dict[str, Any]] = None
        self.started: Optional[float] = None
        self._abort = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._load()

    def _file(self):
        return self.log_dir / "last_campaign.json" if self.log_dir else None

    def _load(self) -> None:
        f = self._file()
        if not f or not f.is_file():
            return
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.state, self.steps, self.report, self.started = d.get("state", "idle"), d.get("steps", []), \
            d.get("report"), d.get("started")
        if self.state in ("planning", "running", "reporting"):   # the dashboard stopped mid-campaign
            self.state = "interrupted"
            for st in self.steps:
                if st.get("status") in ("queued", "waiting", "running"):
                    st["status"] = "interrupted"

    def _save(self) -> None:
        f = self._file()
        if not f:
            return
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"state": self.state, "steps": self.steps, "report": self.report,
                                     "started": self.started}, default=str), encoding="utf-8")
        except OSError:
            pass

    def status(self) -> Dict[str, Any]:
        return {"state": self.state, "steps": [dict(s) for s in self.steps], "report": self.report,
                "started": self.started, **self.runner.status()}

    def start(self, size: int = 4) -> Dict[str, Any]:
        if self.state in ("planning", "running", "reporting") or self.runner.running():
            raise RuntimeError("an experiment or campaign is already running")
        plan = self.plan_fn(size)
        if not plan:
            raise RuntimeError("the planner produced no experiments")
        self.steps = [{"n": i + 1, "id": c["id"], "fault": c["fault"], "target": c["target"],
                       "params": c.get("params") or {}, "reason": c.get("reason", ""), "status": "queued",
                       "result": None} for i, c in enumerate(plan)]
        self.report, self.started, self.state = None, time.time(), "running"
        self._abort.clear()
        self._save()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> Dict[str, Any]:
        self._abort.set()
        out = self.runner.stop("AutopilotEmergencyStop")
        for s in self.steps:
            if s["status"] in ("queued", "waiting", "running"):
                s["status"] = "aborted"
        self.state = "aborted"
        self._save()
        return out

    # ------------------------------------------------------------------ internals
    def _sandbox_healthy(self) -> bool:
        try:
            out = subprocess.run(self.docker_cmd + ["ps", "-a", "--filter", "label=com.chaospilot.role=target",
                                                    "--format", "{{.Status}}"], capture_output=True, text=True,
                                 timeout=10).stdout.strip().splitlines()
        except (OSError, subprocess.TimeoutExpired):
            return False
        return bool(out) and all(l.startswith("Up") and "unhealthy" not in l and "starting" not in l for l in out)

    def _exited(self) -> list:
        try:
            out = subprocess.run(self.docker_cmd + ["ps", "-a", "--filter", "label=com.chaospilot.role=target",
                                                    "--format", "{{.Names}}|{{.Status}}"], capture_output=True, text=True,
                                 timeout=10).stdout.strip().splitlines()
        except (OSError, subprocess.TimeoutExpired):
            return []
        return [n for n, s in (l.split("|", 1) for l in out if "|" in l) if s.startswith(("Exited", "Dead", "Created"))]

    def _heal(self, step: Dict[str, Any]) -> None:
        """A sandbox container that stopped (a cascade the rollback did not undo) is restarted so the campaign goes on."""
        for name in self._exited():
            try:
                subprocess.run(self.docker_cmd + ["start", name], capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                continue
            step.setdefault("healed", []).append(name)
            lines = getattr(self.runner, "lines", None)
            if lines is not None:
                lines.append(f"[autopilot] auto-heal: {name} was still down before step {step['n']}: docker start {name}")

    def _wait_ready(self, step: Dict[str, Any]) -> bool:
        step["status"] = "waiting"
        self._save()
        t0, healed = time.time(), False
        while not self._abort.is_set() and time.time() - t0 < READY_TIMEOUT_S:
            safety = self.runner.safety()
            gate_ok = (not safety["available"]) or safety["is_safe_to_inject"]
            if gate_ok and self._sandbox_healthy():
                return True
            if not healed and time.time() - t0 >= HEAL_AFTER_S:
                healed = True
                self._heal(step)
            step["waiting_for"] = (safety.get("reason") if safety["available"] and not safety["is_safe_to_inject"]
                                   else "sandbox containers to be healthy")
            self._abort.wait(READY_POLL_S)
        return False

    @staticmethod
    def _result(lines: list) -> Dict[str, Any]:
        res = {}
        for l in lines:
            key, _, val = l.partition(":")
            key, val = key.strip(), val.strip()
            if key in ("time to detect", "time to recover", "peak error rate", "recovered", "evidence",
                       "services down", "endpoints failing", "saved", "auto-healed"):
                res[key.replace(" ", "_")] = val
        return res

    def _run(self) -> None:
        try:
            for i, step in enumerate(self.steps):
                if self._abort.is_set():
                    break
                if i > 0:
                    self._abort.wait(COOLDOWN_S)
                if not self._wait_ready(step):
                    step["status"] = "aborted" if self._abort.is_set() else "skipped"
                    step["result"] = {"note": step.get("waiting_for", "aborted")}
                    continue
                step["status"], step["started"] = "running", time.time()
                self._save()
                cand = {"id": step["id"], "fault": step["fault"], "target": step["target"], "params": step["params"]}
                self.runner.start(cand, label=f"autopilot: {step['id']}")
                while self.runner.running():
                    if self._abort.is_set():
                        break
                    time.sleep(1)
                st = self.runner.status()
                step["finished"] = time.time()
                step["result"] = self._result(st["output"])
                code = (st.get("job") or {}).get("exit_code")
                step["status"] = ("aborted" if self._abort.is_set() else
                                  "done" if code == 0 else "failed")
                self._save()
            if self._abort.is_set():
                self.state = "aborted"
                return
            self.state = "reporting"
            if self.report_fn:
                try:
                    r = self.report_fn()
                    self.report = {"ok": True, "generated_at": r.get("generated_at"),
                                   "overall_resilience": r.get("overall_resilience"),
                                   "modes": [t["mode"] for t in r.get("agents", [])]}
                except Exception as exc:  # noqa: BLE001 - the campaign result stands without the report
                    self.report = {"ok": False, "error": str(exc)}
            self.state = "done"
        except Exception as exc:  # noqa: BLE001
            self.state = "failed"
            self.report = {"ok": False, "error": f"campaign error: {exc}"}
        finally:
            self._save()
