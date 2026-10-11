"""
Kill Switch Module for Genesis Chaos Controller (M4 - Safety & Observability)

Non-blocking background safety state machine that:
- Monitors live telemetry against steady-state baseline bands.
- Enforces consecutive window rule: requires breach for 2 consecutive 5-second windows.
- Enforces blind guardrail: aborts immediately if Locust is silent for >= 15 seconds.
- Executes safe, scoped rollback on target sandbox resources only.
- Tracks recovery time in SQLite until metrics stabilize inside baseline bands.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import os
import sys
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

import httpx

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from telemetry import TelemetryCollector, TelemetrySnapshot

try:
    import docker
except ImportError:
    docker = None

logger = logging.getLogger("controller.kill_switch")

# Default Whitelist for Robot Shop Sandbox services to prevent touching unrelated containers
DEFAULT_SANDBOX_SERVICES = [
    "cart",
    "catalogue",
    "dispatch",
    "load",
    "mongodb",
    "mysql",
    "payment",
    "rabbitmq",
    "ratings",
    "redis",
    "shipping",
    "user",
    "web",
]


class KillSwitchState(str, Enum):
    IDLE = "IDLE"                          # No active experiment; observing steady state
    ARMED = "ARMED"                        # Active experiment; within safe bands
    WARNING_WINDOW_1 = "WARNING_WINDOW_1"  # 1st window breached; waiting for confirmation window
    TRIPPED = "TRIPPED"                    # 2 consecutive breaches or blind guardrail; rollback triggered
    RECOVERING = "RECOVERING"              # Rollback completed; monitoring recovery time
    RECOVERED = "RECOVERED"                # Metrics returned to baseline for 3 windows


class TripReason(str, Enum):
    ERROR_RATE_SPIKE = "error_rate_spike"
    LATENCY_DEGRADATION = "latency_degradation"
    BLIND_GUARDRAIL = "blind_guardrail_telemetry_lost"
    CONTAINER_CRITICAL_FAILURE = "container_critical_failure"
    MANUAL = "manual_intervention"


@dataclass
class EvaluationResult:
    state: KillSwitchState
    tripped: bool = False
    reason: Optional[str] = None
    consecutive_breaches: int = 0
    telemetry_gap_seconds: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)


def is_approved_sandbox_container(
    c_name: str,
    labels: Optional[Dict[str, str]],
    sandbox_project: str,
    allowed_services: Set[str],
    container_id: Optional[str] = None,
    allowed_container_ids: Optional[Set[str]] = None,
) -> bool:
    """
    Strictly verifies that a container belongs to the approved Robot Shop sandbox.

    Verification Rules:
    1. If `com.docker.compose.project` is present, it must exactly match `sandbox_project`;
       mismatched project labels are rejected immediately.
    2. Compose-managed containers require BOTH:
       - `com.docker.compose.project == sandbox_project`
       - A valid `com.docker.compose.service` present in `allowed_services`.
       Missing, empty, or unapproved service labels are rejected immediately.
    3. Unlabeled containers (lacking `com.docker.compose.project`) are NEVER approved
       solely based on name prefixes (e.g. 'robot-shop-') or generic service names (e.g. 'cart').
       They are only approved if explicit verifiable ownership is provided via
       `allowed_container_ids` matching `container_id`. Otherwise, rejected.
    """
    labels = labels or {}
    c_project = labels.get("com.docker.compose.project")
    c_service = labels.get("com.docker.compose.service")

    # 1 & 2. Compose-managed containers: require matching project AND valid service
    if c_project:
        if c_project != sandbox_project:
            return False
        if c_service and allowed_services and c_service in allowed_services:
            return True
        return False

    # 3. Unlabeled containers: reject loose names/prefixes; require verifiable container ID allowlist
    if allowed_container_ids and container_id:
        if container_id in allowed_container_ids or container_id[:12] in allowed_container_ids:
            return True

    return False


class KillSwitch:
    """
    Evaluates telemetry against baseline bands and executes emergency rollbacks.
    """

    def __init__(
        self,
        baseline_path: str = "baseline.json",
        db_path: Optional[str] = None,
        telemetry_collector: Optional[TelemetryCollector] = None,
        toxiproxy_url: Optional[str] = None,
        sandbox_project: Optional[str] = None,
        allowed_services: Optional[List[str]] = None,
        allowed_container_ids: Optional[List[str]] = None,
        poll_interval_seconds: float = 5.0,
        blind_guardrail_timeout_seconds: float = 15.0,
        consecutive_windows_required: int = 2,
        session_factory: Optional[Callable[[], Any]] = None,
        rollback_callback: Optional[Callable[[List[int]], Awaitable[List[Dict[str, Any]]]]] = None,
    ):
        self.baseline_path = (
            baseline_path if os.path.isabs(baseline_path) else os.path.join(CURRENT_DIR, baseline_path)
        )
        self.db_path = db_path or os.path.join(CURRENT_DIR, "chaos_state.db")
        self.session_factory = session_factory
        self.rollback_callback = rollback_callback
        self.collector = telemetry_collector or TelemetryCollector()
        self.toxiproxy_url = (
            toxiproxy_url or os.environ.get("TOXIPROXY_URL", "http://localhost:8474")
        ).rstrip("/")
        self.sandbox_project = sandbox_project or os.environ.get("SANDBOX_PROJECT", "robot-shop")
        self.allowed_services = set(allowed_services or DEFAULT_SANDBOX_SERVICES)
        env_cids = [
            cid.strip()
            for cid in os.environ.get("ALLOWED_CONTAINER_IDS", "").split(",")
            if cid.strip()
        ]
        self.allowed_container_ids: Set[str] = set(allowed_container_ids or env_cids)
        self.poll_interval = poll_interval_seconds
        self.blind_guardrail_timeout = blind_guardrail_timeout_seconds
        self.consecutive_windows_required = consecutive_windows_required

        # State Tracking
        self.state = KillSwitchState.IDLE
        self.consecutive_breaches = 0
        self.last_valid_telemetry_time = time.time()
        self.recovery_consecutive_normal_windows = 0
        self.active_intent_id: Optional[int] = None
        self.active_intent_ids: List[int] = []
        self.last_snapshot: Optional[TelemetrySnapshot] = None
        self.last_trip_reason: Optional[str] = None
        self.last_evaluation = EvaluationResult(state=KillSwitchState.IDLE)
        self.rollback_executed_at: Optional[float] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Baseline thresholds loaded in memory
        self.baseline_bands: Optional[Dict[str, Any]] = None
        self._baseline_mtime: Optional[float] = None
        self.load_baseline()

    def load_baseline(self) -> Optional[Dict[str, Any]]:
        """Loads or reloads baseline.json if present on disk."""
        if os.path.exists(self.baseline_path):
            try:
                mtime = os.path.getmtime(self.baseline_path)
                if self.baseline_bands is not None and self._baseline_mtime == mtime:
                    return self.baseline_bands
                with open(self.baseline_path, "r", encoding="utf-8") as f:
                    self.baseline_bands = json.load(f)
                self._baseline_mtime = mtime
                logger.info("Kill Switch loaded baseline from %s (mtime=%.1f)", self.baseline_path, mtime)
                return self.baseline_bands
            except Exception as exc:
                logger.warning("Failed to load baseline from %s: %s", self.baseline_path, exc)
        return None

    def get_thresholds(self) -> Tuple[float, float]:
        """
        Returns (max_error_rate, max_p95_latency_ms).
        Defaults: error_rate > 0.05 (5%), p95 > 5 * baseline_p95 (or 250ms fallback).
        Automatically reloads baseline if baseline.json was created or modified on disk.
        """
        if os.path.exists(self.baseline_path):
            current_mtime = os.path.getmtime(self.baseline_path)
            if self.baseline_bands is None or self._baseline_mtime != current_mtime:
                self.load_baseline()

        if self.baseline_bands:
            thresholds = self.baseline_bands.get("kill_switch_thresholds", {})
            max_err = thresholds.get("max_error_rate", 0.05)
            p95_limit = thresholds.get("p95_latency_threshold_ms")
            if p95_limit is not None:
                return float(max_err), float(p95_limit)

            p95_mean = self.baseline_bands.get("p95_latency_ms", {}).get("mean", 50.0)
            return float(max_err), float(5.0 * p95_mean)

        return 0.05, 500.0

    def evaluate_snapshot(
        self,
        snapshot: TelemetrySnapshot,
        has_active_chaos: bool = False,
        now: Optional[float] = None,
    ) -> EvaluationResult:
        """
        Pure evaluation function for a single 5s telemetry window.
        """
        current_time = time.time() if now is None else now
        max_error_rate, max_p95_latency = self.get_thresholds()

        # 1. Blind Guardrail Check: Has Locust telemetry vanished for >= 15 seconds?
        if snapshot.locust.available:
            self.last_valid_telemetry_time = current_time
            gap_seconds = 0.0
        else:
            gap_seconds = current_time - self.last_valid_telemetry_time
            if gap_seconds >= self.blind_guardrail_timeout:
                self.state = KillSwitchState.TRIPPED
                return EvaluationResult(
                    state=KillSwitchState.TRIPPED,
                    tripped=True,
                    reason=TripReason.BLIND_GUARDRAIL.value,
                    consecutive_breaches=self.consecutive_breaches,
                    telemetry_gap_seconds=gap_seconds,
                    details={
                        "message": f"Locust telemetry absent for {gap_seconds:.1f}s (>= {self.blind_guardrail_timeout}s limit)",
                        "error": snapshot.locust.error_message,
                    },
                )

        # If already recovering, evaluate if metrics returned to safe bands
        if self.state in (KillSwitchState.TRIPPED, KillSwitchState.RECOVERING):
            is_normal = (
                snapshot.locust.available
                and snapshot.error_rate is not None
                and snapshot.error_rate <= max_error_rate
                and snapshot.p95_latency is not None
                and snapshot.p95_latency <= max_p95_latency
            )
            if is_normal:
                self.recovery_consecutive_normal_windows += 1
                if self.recovery_consecutive_normal_windows >= 3:
                    self.state = KillSwitchState.RECOVERED
            else:
                self.recovery_consecutive_normal_windows = 0

            return EvaluationResult(
                state=self.state,
                tripped=False,
                consecutive_breaches=0,
                telemetry_gap_seconds=gap_seconds,
                details={"recovery_windows": self.recovery_consecutive_normal_windows},
            )

        # 2. Check metric breaches
        breach_reasons: List[str] = []
        if snapshot.locust.available:
            if snapshot.error_rate is not None and snapshot.error_rate > max_error_rate:
                breach_reasons.append(
                    f"error_rate {snapshot.error_rate:.2%} > threshold {max_error_rate:.2%}"
                )
            if snapshot.p95_latency is not None and snapshot.p95_latency > max_p95_latency:
                breach_reasons.append(
                    f"p95 {snapshot.p95_latency:.1f}ms > threshold {max_p95_latency:.1f}ms"
                )

        if breach_reasons:
            self.consecutive_breaches += 1
            if self.consecutive_breaches >= self.consecutive_windows_required:
                self.state = KillSwitchState.TRIPPED
                return EvaluationResult(
                    state=KillSwitchState.TRIPPED,
                    tripped=True,
                    reason="; ".join(breach_reasons),
                    consecutive_breaches=self.consecutive_breaches,
                    telemetry_gap_seconds=gap_seconds,
                    details={"breaches": breach_reasons},
                )
            else:
                self.state = KillSwitchState.WARNING_WINDOW_1
                return EvaluationResult(
                    state=KillSwitchState.WARNING_WINDOW_1,
                    tripped=False,
                    reason="; ".join(breach_reasons),
                    consecutive_breaches=self.consecutive_breaches,
                    telemetry_gap_seconds=gap_seconds,
                    details={"warning": "Window 1 breached, awaiting consecutive window confirmation"},
                )
        else:
            # Metrics normal: reset consecutive breaches (filters random transient spikes)
            self.consecutive_breaches = 0
            self.state = KillSwitchState.ARMED if has_active_chaos else KillSwitchState.IDLE
            return EvaluationResult(
                state=self.state,
                tripped=False,
                consecutive_breaches=0,
                telemetry_gap_seconds=gap_seconds,
            )

    async def execute_safe_rollback(
        self,
        intent_id: Optional[int] = None,
        reason: Optional[str] = None,
        intent_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """
        Executes safe recovery strictly bounded to sandbox resources:
        1. Issues docker start to stopped sandbox containers only.
        2. Clears all Toxiproxy toxics on the sandbox network.
        3. Marks intent in SQLite as aborted.
        4. Records kill_switch_fired=True in ExperimentResult.
        """
        logger.warning(
            "KILL SWITCH TRIPPED: Initiating safe emergency rollback. Reason: %s", reason
        )
        self.rollback_executed_at = time.time()
        results: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "containers_restarted": [],
            "toxics_cleared": 0,
            "intent_aborted": False,
        }

        targets = list(intent_ids or ([intent_id] if intent_id is not None else self._get_active_intent_ids()))
        results["intent_ids"] = targets

        # Let each fault injector undo its own changes (disk files, toxics, container state).
        if self.rollback_callback and targets:
            try:
                results["intent_rollbacks"] = await self.rollback_callback(targets)
            except Exception as exc:
                logger.error("Fault-specific rollback failed: %s", exc)
                results["rollback_callback_error"] = str(exc)

        # 1. Scope-bounded Docker container recovery
        if docker is not None:
            try:
                loop = asyncio.get_running_loop()
                restarted = await loop.run_in_executor(None, self._sync_revive_sandbox_containers)
                results["containers_restarted"] = restarted
            except Exception as exc:
                logger.error("Failed to revive sandbox containers: %s", exc)
                results["docker_error"] = str(exc)

        # 2. Reset Toxiproxy toxics
        try:
            cleared_count = await self._clear_all_toxiproxy_toxics()
            results["toxics_cleared"] = cleared_count
        except Exception as exc:
            logger.error("Failed to clear Toxiproxy toxics: %s", exc)
            results["toxiproxy_error"] = str(exc)

        # 3. SQLite updates
        try:
            self._update_db_intent_aborted(targets, reason=reason)
            results["intent_aborted"] = True
        except Exception as exc:
            logger.error("Failed to record abort in SQLite: %s", exc)
            results["db_error"] = str(exc)

        return results

    def _sync_revive_sandbox_containers(self) -> List[str]:
        """
        Safely identifies and restarts stopped containers strictly belonging to the sandbox.
        Never restarts or touches unrelated containers.
        """
        if docker is None:
            return []

        try:
            client = docker.from_env()
            containers = client.containers.list(all=True)
        except Exception as exc:
            logger.warning("Docker unreachable during rollback: %s", exc)
            return []

        restarted: List[str] = []
        for c in containers:
            c_name = c.name
            labels = c.labels or {}
            c_id = getattr(c, "id", None)

            # Check if container belongs strictly to sandbox
            if not is_approved_sandbox_container(
                c_name,
                labels,
                self.sandbox_project,
                self.allowed_services,
                container_id=c_id,
                allowed_container_ids=self.allowed_container_ids,
            ):
                continue

            if c.status != "running":
                try:
                    logger.info("Watchdog/KillSwitch starting stopped sandbox container: %s", c_name)
                    c.start()
                    restarted.append(c_name)
                except Exception as exc:
                    logger.error("Failed to start sandbox container %s: %s", c_name, exc)

        return restarted

    async def _clear_all_toxiproxy_toxics(self) -> int:
        """
        Queries Toxiproxy API and deletes all active toxics.
        """
        total_deleted = 0
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self.toxiproxy_url}/proxies")
                if resp.status_code != 200:
                    return 0
                proxies = resp.json()
                for proxy_name in proxies.keys():
                    # Get list of toxics for this proxy
                    t_resp = await client.get(f"{self.toxiproxy_url}/proxies/{proxy_name}/toxics")
                    if t_resp.status_code == 200:
                        toxics = t_resp.json()
                        for t in toxics:
                            t_name = t.get("name")
                            if t_name:
                                del_resp = await client.delete(
                                    f"{self.toxiproxy_url}/proxies/{proxy_name}/toxics/{t_name}"
                                )
                                if del_resp.status_code in (200, 204):
                                    total_deleted += 1
                                    logger.info(
                                        "Deleted toxic '%s' on proxy '%s'", t_name, proxy_name
                                    )
        except httpx.RequestError:
            # Toxiproxy might not be deployed yet; log softly
            logger.info("Toxiproxy API not reachable at %s (skipping toxic purge)", self.toxiproxy_url)
        return total_deleted

    def _get_active_intent_ids(self) -> List[int]:
        if not self.session_factory:
            return []
        try:
            from models import ExperimentIntent

            session = self.session_factory()
            try:
                return [
                    row.id
                    for row in session.query(ExperimentIntent.id)
                    .filter(ExperimentIntent.status.in_(["pending", "running"]))
                    .order_by(ExperimentIntent.id)
                    .all()
                ]
            finally:
                session.close()
        except Exception as exc:
            logger.error("Could not read active experiment intents: %s", exc)
            return []

    def _update_db_intent_aborted(
        self,
        intent_ids: Optional[List[int]] = None,
        reason: Optional[str] = None,
        intent_id: Optional[int] = None,
    ) -> None:
        """
        Marks active or pending intents in SQLite as aborted, and logs kill_switch_fired=True.
        """
        try:
            from models import Base, ExperimentIntent, ExperimentResult

            engine = None
            if self.session_factory:
                session = self.session_factory()
            else:
                from sqlalchemy import create_engine
                from sqlalchemy.orm import sessionmaker

                engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False})
                Base.metadata.create_all(bind=engine)
                session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()

            try:
                # Find target intent
                if intent_id is not None and not intent_ids:
                    intent_ids = [intent_id]
                query = session.query(ExperimentIntent)
                if intent_ids:
                    intents = query.filter(ExperimentIntent.id.in_(intent_ids)).all()
                else:
                    intents = query.filter(ExperimentIntent.status.in_(["pending", "running"])).all()

                for intent in intents:
                    intent.status = "aborted"
                    # Create or update experiment result
                    result = (
                        session.query(ExperimentResult)
                        .filter(ExperimentResult.intent_id == intent.id)
                        .first()
                    )
                    if not result:
                        result = ExperimentResult(
                            intent_id=intent.id,
                            kill_switch_fired=True,
                            recovery_time_sec=None,
                            resilience_score=0.0,
                            target_service=intent.target_service,
                            details=json.dumps({"kill_switch_reason": reason}) if reason else "{}",
                        )
                        session.add(result)
                    else:
                        result.kill_switch_fired = True
                        result.target_service = intent.target_service
                        result.details = json.dumps({"kill_switch_reason": reason}) if reason else result.details

                session.commit()
            finally:
                session.close()
                if engine is not None:
                    engine.dispose()
        except Exception as exc:
            logger.info("SQLite record update skipped: %s", exc)

    async def monitor_recovery_period(
        self,
        intent_id: Optional[int] = None,
        timeout_seconds: float = 60.0,
    ) -> float:
        """
        Tracks the time taken from rollback until metrics re-enter baseline bands for 3 consecutive windows.
        Records recovery_time_sec into SQLite ExperimentResult.
        """
        if not self.rollback_executed_at:
            self.rollback_executed_at = time.time()

        start_time = self.rollback_executed_at
        consecutive_stable = 0
        max_error_rate, max_p95_latency = self.get_thresholds()

        while (time.time() - start_time) < timeout_seconds:
            await asyncio.sleep(self.poll_interval)
            snapshot = await self.collector.collect_snapshot()

            is_stable = (
                snapshot.locust.available
                and snapshot.error_rate is not None
                and snapshot.error_rate <= max_error_rate
                and snapshot.p95_latency is not None
                and snapshot.p95_latency <= max_p95_latency
            )

            if is_stable:
                consecutive_stable += 1
                if consecutive_stable >= 3:
                    recovery_time = round(time.time() - start_time, 2)
                    logger.info("System recovered to baseline in %.2f seconds", recovery_time)
                    self._update_db_recovery_time(intent_id, recovery_time)
                    self.state = KillSwitchState.RECOVERED
                    return recovery_time
            else:
                consecutive_stable = 0

        total_elapsed = round(time.time() - start_time, 2)
        logger.warning("Recovery period timed out after %.2fs without 3 stable windows", total_elapsed)
        self._update_db_recovery_time(intent_id, total_elapsed)
        return total_elapsed

    def _update_db_recovery_time(self, intent_id: Optional[int], recovery_time: float) -> None:
        if not intent_id:
            return
        try:
            from models import ExperimentResult

            engine = None
            if self.session_factory:
                session = self.session_factory()
            else:
                from sqlalchemy import create_engine
                from sqlalchemy.orm import sessionmaker

                engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False})
                session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
            try:
                res = (
                    session.query(ExperimentResult)
                    .filter(ExperimentResult.intent_id == intent_id)
                    .first()
                )
                if res:
                    res.recovery_time_sec = recovery_time
                    session.commit()
            finally:
                session.close()
                if engine is not None:
                    engine.dispose()
        except Exception:
            pass

    async def step(self) -> EvaluationResult:
        """
        Performs a single evaluation step.
        """
        snapshot = await self.collector.collect_snapshot()
        self.last_snapshot = snapshot
        if self.session_factory and snapshot.error_rate is not None and snapshot.p95_latency is not None:
            session = self.session_factory()
            try:
                TelemetryCollector.persist_snapshot(snapshot, session, is_baseline=False)
            finally:
                session.close()

        self.active_intent_ids = self._get_active_intent_ids()
        self.active_intent_id = self.active_intent_ids[0] if self.active_intent_ids else None
        if self.active_intent_ids:
            if self.state in (KillSwitchState.IDLE, KillSwitchState.RECOVERED):
                self.state = KillSwitchState.ARMED
                self.recovery_consecutive_normal_windows = 0
        elif self.state not in (KillSwitchState.TRIPPED, KillSwitchState.RECOVERING, KillSwitchState.RECOVERED):
            self.state = KillSwitchState.IDLE
            self.last_evaluation = EvaluationResult(state=self.state)
            return self.last_evaluation

        eval_res = self.evaluate_snapshot(snapshot, has_active_chaos=bool(self.active_intent_ids))
        self.last_evaluation = eval_res

        if eval_res.tripped:
            self.last_trip_reason = eval_res.reason
            affected_intents = list(self.active_intent_ids)
            await self.execute_safe_rollback(
                intent_ids=affected_intents, reason=eval_res.reason
            )
            if affected_intents:
                asyncio.create_task(self.monitor_recovery_period(affected_intents[0]))
            self.active_intent_ids = []
            self.active_intent_id = None

        return eval_res

    async def manual_trip(self, reason: str = "Manual Override") -> Dict[str, Any]:
        """Trips the guardrail and rolls back every currently active sandbox experiment."""
        self.last_trip_reason = reason
        self.state = KillSwitchState.TRIPPED
        intent_ids = self._get_active_intent_ids()
        self.active_intent_ids = intent_ids
        self.active_intent_id = intent_ids[0] if intent_ids else None
        result = await self.execute_safe_rollback(intent_ids=intent_ids, reason=reason)
        if intent_ids:
            asyncio.create_task(self.monitor_recovery_period(intent_ids[0]))
        self.active_intent_ids = []
        self.active_intent_id = None
        return result

    async def run_loop(self) -> None:
        """Continuous background loop for FastAPI."""
        self._running = True
        logger.info("Kill Switch background loop started (interval=%.1fs)", self.poll_interval)
        try:
            while self._running:
                try:
                    await self.step()
                except Exception as exc:
                    logger.error("Error during Kill Switch evaluation cycle: %s", exc)
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            logger.info("Kill Switch background loop stopped.")
        finally:
            self._running = False

    def start_background(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self.run_loop())
        return self._task

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
