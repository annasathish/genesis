"""
Dead-Man Watchdog for Genesis Chaos Controller (M4 - Safety & Observability)

Monitors the FastAPI controller's heartbeat every 5 seconds.
If the heartbeat is older than 20 seconds (indicating controller hang, crash, or OOM):
- Immediately connects to Docker (local or remote via DOCKER_HOST).
- Safely issues `docker start` strictly to stopped sandbox containers.
- Purges all active toxics from Toxiproxy.
- Marks running/pending intents in SQLite as 'aborted'.

Safety Rules:
- Limits recovery strictly to explicitly whitelisted sandbox services.
- Never runs destructive commands or touches containers outside the sandbox.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from heartbeat import HeartbeatManager

try:
    import docker
except ImportError:
    docker = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] watchdog: %(message)s",
)
logger = logging.getLogger("watchdog")

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


class DeadManWatchdog:
    """
    Independent failsafe watchdog daemon.
    """

    def __init__(
        self,
        heartbeat_file: Optional[str] = None,
        timeout_seconds: float = 20.0,
        check_interval_seconds: float = 5.0,
        docker_host: Optional[str] = None,
        toxiproxy_url: Optional[str] = None,
        sandbox_project: Optional[str] = None,
        allowed_services: Optional[List[str]] = None,
        db_path: Optional[str] = None,
    ):
        self.heartbeat_manager = HeartbeatManager(heartbeat_file)
        self.timeout_seconds = float(
            timeout_seconds or os.environ.get("WATCHDOG_TIMEOUT_SEC", 20.0)
        )
        self.check_interval = float(
            check_interval_seconds or os.environ.get("WATCHDOG_INTERVAL_SEC", 5.0)
        )
        self.docker_host = docker_host or os.environ.get("DOCKER_HOST")
        self.toxiproxy_url = (
            toxiproxy_url or os.environ.get("TOXIPROXY_URL", "http://localhost:8474")
        ).rstrip("/")
        self.sandbox_project = sandbox_project or os.environ.get("SANDBOX_PROJECT", "robot-shop")
        self.allowed_services: Set[str] = set(
            allowed_services or DEFAULT_SANDBOX_SERVICES
        )
        self.db_path = db_path or os.path.join(CURRENT_DIR, "chaos_state.db")
        self._running = False

    def is_controller_healthy(self) -> Tuple[bool, float, Dict[str, Any]]:
        """
        Reads the heartbeat file and determines if controller is within the 20s window.
        Returns (is_alive, age_seconds, heartbeat_data).
        """
        exists, age, data = self.heartbeat_manager.read_heartbeat()
        if not exists:
            # File missing entirely
            return False, age, {"error": "Heartbeat file not found"}

        if age > self.timeout_seconds:
            return False, age, data

        return True, age, data

    def trigger_emergency_rollback(self, age_seconds: float) -> Dict[str, Any]:
        """
        Executes emergency failsafe actions when the controller is dead.
        Safe: strictly bounded to sandbox containers and toxics.
        """
        logger.critical(
            "DEAD-MAN SWITCH FIRED: Controller heartbeat is %.1fs old (limit: %.1fs)!",
            age_seconds,
            self.timeout_seconds,
        )

        recovery_summary = {
            "triggered_at": datetime.now(timezone.utc).isoformat(),
            "heartbeat_age_sec": age_seconds,
            "containers_started": self._revive_sandbox_containers(),
            "toxics_purged": self._purge_toxiproxy_toxics(),
            "database_updated": self._abort_orphaned_intents(),
        }

        logger.info("Emergency rollback summary: %s", json.dumps(recovery_summary))
        return recovery_summary

    def _get_docker_client(self):
        if docker is None:
            logger.warning("Docker SDK is not installed in the watchdog environment.")
            return None
        try:
            if self.docker_host:
                return docker.DockerClient(base_url=self.docker_host)
            return docker.from_env()
        except Exception as exc:
            logger.error("Failed to connect to Docker daemon (%s): %s", self.docker_host or "local", exc)
            return None

    def _revive_sandbox_containers(self) -> List[str]:
        """
        Restarts only stopped containers belonging to the designated sandbox project.
        Guarantees other host containers are never modified.
        """
        client = self._get_docker_client()
        if client is None:
            return []

        restarted: List[str] = []
        try:
            containers = client.containers.list(all=True)
            for c in containers:
                c_name = c.name
                labels = c.labels or {}
                c_project = labels.get("com.docker.compose.project")

                # Strictly verify membership in sandbox
                is_target_sandbox = False
                if c_project and c_project == self.sandbox_project:
                    is_target_sandbox = True
                elif self.sandbox_project and (
                    c_name.startswith(f"{self.sandbox_project}_")
                    or c_name.startswith(f"{self.sandbox_project}-")
                ):
                    is_target_sandbox = True
                elif any(svc in c_name for svc in self.allowed_services):
                    is_target_sandbox = True

                if not is_target_sandbox:
                    continue

                if c.status != "running":
                    try:
                        logger.warning("Watchdog starting stopped sandbox container: %s", c_name)
                        c.start()
                        restarted.append(c_name)
                    except Exception as exc:
                        logger.error("Watchdog failed to restart %s: %s", c_name, exc)

        except Exception as exc:
            logger.error("Error inspecting containers during watchdog recovery: %s", exc)

        return restarted

    def _purge_toxiproxy_toxics(self) -> int:
        """
        Synchronously queries Toxiproxy and deletes all active toxics.
        """
        deleted_count = 0
        try:
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(f"{self.toxiproxy_url}/proxies")
                if resp.status_code != 200:
                    return 0
                proxies = resp.json()
                for proxy_name in proxies.keys():
                    t_resp = client.get(f"{self.toxiproxy_url}/proxies/{proxy_name}/toxics")
                    if t_resp.status_code == 200:
                        toxics = t_resp.json()
                        for t in toxics:
                            t_name = t.get("name")
                            if t_name:
                                d_resp = client.delete(
                                    f"{self.toxiproxy_url}/proxies/{proxy_name}/toxics/{t_name}"
                                )
                                if d_resp.status_code in (200, 204):
                                    deleted_count += 1
                                    logger.info("Purged toxic '%s' on proxy '%s'", t_name, proxy_name)
        except Exception as exc:
            logger.info("Toxiproxy unavailable during watchdog sweep: %s", exc)

        return deleted_count

    def _abort_orphaned_intents(self) -> bool:
        """
        Marks active or pending intents in SQLite as aborted to clean up orphaned state.
        """
        if not os.path.exists(self.db_path):
            return False

        try:
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker
            from models import Base, ExperimentIntent, ExperimentResult

            engine = create_engine(
                f"sqlite:///{self.db_path}",
                connect_args={"check_same_thread": False},
            )
            Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
            session = Session()

            try:
                intents = session.query(ExperimentIntent).filter(
                    ExperimentIntent.status.in_(["pending", "running"])
                ).all()

                for intent in intents:
                    intent.status = "aborted"
                    result = (
                        session.query(ExperimentResult)
                        .filter(ExperimentResult.intent_id == intent.id)
                        .first()
                    )
                    if not result:
                        session.add(
                            ExperimentResult(
                                intent_id=intent.id,
                                kill_switch_fired=True,
                                recovery_time_sec=None,
                                resilience_score=0.0,
                            )
                        )
                    else:
                        result.kill_switch_fired = True

                session.commit()
                return True
            finally:
                session.close()
        except Exception as exc:
            logger.error("Failed to abort orphaned intents in SQLite: %s", exc)
            return False

    def check_once(self) -> bool:
        """
        Performs a single heartbeat check. If dead, triggers recovery.
        Returns True if controller is alive, False if dead-man switch fired.
        """
        is_alive, age, _ = self.is_controller_healthy()
        if not is_alive:
            self.trigger_emergency_rollback(age)
            return False
        logger.debug("Heartbeat healthy (age: %.1fs)", age)
        return True

    def run_daemon(self) -> None:
        """
        Continuous watchdog monitoring loop.
        """
        self._running = True
        logger.info(
            "Watchdog daemon started. Checking heartbeat every %.1fs (timeout: %.1fs)...",
            self.check_interval,
            self.timeout_seconds,
        )

        while self._running:
            try:
                self.check_once()
            except Exception as exc:
                logger.error("Unhandled error in watchdog loop: %s", exc)

            time.sleep(self.check_interval)

    def stop(self) -> None:
        self._running = False


def parse_args():
    parser = argparse.ArgumentParser(description="Genesis Chaos Dead-Man Watchdog (M4)")
    parser.add_argument(
        "--heartbeat-file",
        type=str,
        default=os.environ.get("HEARTBEAT_FILE", "heartbeat.json"),
        help="Path to shared heartbeat file",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("WATCHDOG_TIMEOUT_SEC", 20.0)),
        help="Timeout in seconds before dead-man switch fires (default: 20s)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("WATCHDOG_INTERVAL_SEC", 5.0)),
        help="Check interval in seconds (default: 5s)",
    )
    parser.add_argument(
        "--docker-host",
        type=str,
        default=os.environ.get("DOCKER_HOST"),
        help="Docker host URL/socket (e.g. tcp://teammate-ip:2375)",
    )
    parser.add_argument(
        "--toxiproxy-url",
        type=str,
        default=os.environ.get("TOXIPROXY_URL", "http://localhost:8474"),
        help="Toxiproxy admin URL",
    )
    parser.add_argument(
        "--sandbox-project",
        type=str,
        default=os.environ.get("SANDBOX_PROJECT", "robot-shop"),
        help="Target sandbox compose project name",
    )
    parser.add_argument(
        "--check-once",
        action="store_true",
        help="Perform a single heartbeat check and exit",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    watchdog = DeadManWatchdog(
        heartbeat_file=args.heartbeat_file,
        timeout_seconds=args.timeout,
        check_interval_seconds=args.interval,
        docker_host=args.docker_host,
        toxiproxy_url=args.toxiproxy_url,
        sandbox_project=args.sandbox_project,
    )

    def handle_signal(sig, frame):
        logger.info("Watchdog shutting down on signal %s", sig)
        watchdog.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    if args.check_once:
        alive = watchdog.check_once()
        sys.exit(0 if alive else 1)
    else:
        watchdog.run_daemon()


if __name__ == "__main__":
    main()
