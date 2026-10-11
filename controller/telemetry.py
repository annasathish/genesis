"""
Telemetry Ingestion Module for Genesis Chaos Controller (M4 - Safety & Observability)

Rely on Locust for user-facing metrics (error rate, p95 latency) and Docker SDK
for container health. Collects real telemetry asynchronously without blocking
the event loop. Gracefully handles unavailable services, timeouts, and missing data.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

try:
    import docker
    from docker.errors import DockerException
except ImportError:
    docker = None
    DockerException = Exception

logger = logging.getLogger("controller.telemetry")


@dataclass
class LocustMetrics:
    """Represents real metrics parsed from Locust /stats/requests."""
    available: bool
    state: str = "unknown"
    error_rate: Optional[float] = None       # Ratio between 0.0 and 1.0 (e.g. 0.02 = 2%)
    p95_latency: Optional[float] = None      # 95th percentile latency in milliseconds
    current_rps: float = 0.0
    num_requests: int = 0
    num_failures: int = 0
    error_message: Optional[str] = None
    raw_aggregated: Optional[Dict[str, Any]] = None


@dataclass
class ContainerHealth:
    """Represents the operational state of a monitored Docker container."""
    name: str
    status: str                              # e.g. 'running', 'exited', 'restarting'
    health_status: Optional[str] = None      # e.g. 'healthy', 'unhealthy', 'starting'
    restart_count: int = 0


@dataclass
class DockerMetrics:
    """Represents Docker sandbox container health."""
    available: bool
    containers: Dict[str, ContainerHealth] = field(default_factory=dict)
    all_running: bool = True
    error_message: Optional[str] = None


@dataclass
class TelemetrySnapshot:
    """Unified telemetry observation for a single time window."""
    timestamp: datetime
    locust: LocustMetrics
    docker: DockerMetrics
    error_rate: Optional[float]
    p95_latency: Optional[float]
    is_healthy: bool
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "is_healthy": self.is_healthy,
            "error_rate": self.error_rate,
            "p95_latency": self.p95_latency,
            "error_message": self.error_message,
            "locust": {
                "available": self.locust.available,
                "state": self.locust.state,
                "error_rate": self.locust.error_rate,
                "p95_latency": self.locust.p95_latency,
                "current_rps": self.locust.current_rps,
                "num_requests": self.locust.num_requests,
                "num_failures": self.locust.num_failures,
                "error_message": self.locust.error_message,
            },
            "docker": {
                "available": self.docker.available,
                "all_running": self.docker.all_running,
                "error_message": self.docker.error_message,
                "containers": {
                    k: {
                        "name": v.name,
                        "status": v.status,
                        "health_status": v.health_status,
                        "restart_count": v.restart_count,
                    }
                    for k, v in self.docker.containers.items()
                },
            },
        }


class LocustClient:
    """
    Asynchronous client for polling Locust load generator statistics.
    Default endpoint: http://localhost:8089 or http://load:8089 (sandbox load container).
    Configurable via LOCUST_URL environment variable or base_url constructor argument.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout_seconds: float = 3.0,
    ):
        raw_url = base_url or os.environ.get("LOCUST_URL", "http://localhost:8089")
        self.base_url = raw_url.rstrip("/")
        self.timeout_seconds = float(os.environ.get("LOCUST_TIMEOUT_SEC", timeout_seconds))

    async def fetch_stats(self) -> LocustMetrics:
        """
        Polls /stats/requests on Locust. Extracts total error rate and p95 latency.
        Does NOT fabricate measurements if Locust is offline or unreachable.
        """
        stats_url = f"{self.base_url}/stats/requests"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.get(stats_url)
                if resp.status_code != 200:
                    return LocustMetrics(
                        available=False,
                        error_message=f"Locust returned HTTP {resp.status_code}",
                    )
                data = resp.json()
                return self._parse_locust_data(data)
        except httpx.RequestError as exc:
            logger.warning("Locust request failed at %s: %s", stats_url, exc)
            return LocustMetrics(
                available=False,
                error_message=f"Locust connection failed: {exc}",
            )
        except Exception as exc:
            logger.error("Unexpected error parsing Locust response: %s", exc)
            return LocustMetrics(
                available=False,
                error_message=f"Locust parsing error: {exc}",
            )

    def _parse_locust_data(self, data: Dict[str, Any]) -> LocustMetrics:
        state = str(data.get("state", "unknown"))
        stats_list = data.get("stats", [])

        # Locate aggregated row
        aggregated_row = None
        for row in stats_list:
            name = str(row.get("name", "")).strip().lower()
            if name in ("aggregated", "total"):
                aggregated_row = row
                break

        if aggregated_row is None and stats_list:
            # If no aggregated row provided, pick the largest or summarize
            aggregated_row = stats_list[-1]

        if not aggregated_row:
            # Locust running but hasn't generated any stats yet
            return LocustMetrics(
                available=True,
                state=state,
                error_rate=0.0,
                p95_latency=0.0,
                current_rps=float(data.get("total_rps", 0.0)),
                num_requests=0,
                num_failures=0,
            )

        num_requests = int(aggregated_row.get("num_requests", 0))
        num_failures = int(aggregated_row.get("num_failures", 0))

        # Calculate error rate (ratio in [0.0, 1.0])
        if num_requests > 0:
            error_rate = num_failures / num_requests
        else:
            top_fail_ratio = data.get("fail_ratio")
            error_rate = float(top_fail_ratio) if top_fail_ratio is not None else 0.0

        p95_latency = self._extract_p95(aggregated_row)
        current_rps = float(aggregated_row.get("current_rps", data.get("total_rps", 0.0)))

        return LocustMetrics(
            available=True,
            state=state,
            error_rate=error_rate,
            p95_latency=p95_latency,
            current_rps=current_rps,
            num_requests=num_requests,
            num_failures=num_failures,
            raw_aggregated=aggregated_row,
        )

    @staticmethod
    def _extract_p95(agg_row: Dict[str, Any]) -> float:
        """
        Extracts 95th percentile response time in milliseconds with multi-version fallbacks.
        """
        # 1. Standard Locust percentile keys
        for key in (
            "current_response_time_percentile_95",
            "response_time_percentile_0.95",
            "percentile_95",
            "p95",
        ):
            val = agg_row.get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass

        # 2. Key inside response_time_percentiles dict
        percentiles_dict = agg_row.get("response_time_percentiles")
        if isinstance(percentiles_dict, dict):
            for k in ("0.95", 0.95, "95%", "95"):
                if k in percentiles_dict:
                    try:
                        return float(percentiles_dict[k])
                    except (ValueError, TypeError):
                        pass

        # 3. Calculate from response_times histogram if present
        hist = agg_row.get("response_times")
        if isinstance(hist, dict) and hist:
            try:
                sorted_bins = sorted(
                    ((float(ms), int(cnt)) for ms, cnt in hist.items()),
                    key=lambda item: item[0],
                )
                total_samples = sum(cnt for _, cnt in sorted_bins)
                if total_samples > 0:
                    threshold = 0.95 * total_samples
                    cumulative = 0
                    for ms, cnt in sorted_bins:
                        cumulative += cnt
                        if cumulative >= threshold:
                            return float(ms)
            except Exception:
                pass

        # 4. Fallback to 99th or avg/median if 95th is absent
        for fallback_key in ("ninetynine_response_time", "avg_response_time", "median_response_time"):
            val = agg_row.get(fallback_key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass

        return 0.0


class DockerMonitor:
    """
    Asynchronous Docker container monitor wrapped with run_in_executor
    to prevent event loop blocking on Windows / WSL2 host.
    Configurable to connect to local or remote Docker daemon (via DOCKER_HOST).
    """

    def __init__(
        self,
        project_name: Optional[str] = None,
        container_prefix: Optional[str] = None,
        docker_host: Optional[str] = None,
    ):
        self.project_name = project_name or os.environ.get("SANDBOX_PROJECT", "robot-shop")
        self.container_prefix = container_prefix or os.environ.get("CONTAINER_PREFIX")
        self.docker_host = docker_host or os.environ.get("DOCKER_HOST")

    def _sync_get_containers(self) -> DockerMetrics:
        if docker is None:
            return DockerMetrics(
                available=False,
                error_message="docker SDK module not installed",
            )

        try:
            if self.docker_host:
                client = docker.DockerClient(base_url=self.docker_host)
            else:
                client = docker.from_env()
            # Test connectivity
            client.ping()
        except Exception as exc:
            return DockerMetrics(
                available=False,
                error_message=f"Docker daemon unavailable ({self.docker_host or 'local'}): {exc}",
            )

        try:
            containers = client.containers.list(all=True)
            result: Dict[str, ContainerHealth] = {}
            all_running = True

            for c in containers:
                c_name = c.name
                # Filter if project_name or container_prefix is specified
                labels = c.labels or {}
                c_project = labels.get("com.docker.compose.project")

                matched = True
                if self.project_name:
                    # a container without a compose project label (e.g. an unrelated local container) is not ours
                    matched = (c_project == self.project_name)
                elif self.container_prefix:
                    matched = c_name.startswith(self.container_prefix)

                if not matched:
                    continue

                status = c.status
                state = c.attrs.get("State", {})
                health_dict = state.get("Health") or {}
                health_status = health_dict.get("Status")
                restart_count = int(c.attrs.get("RestartCount", 0))

                if status != "running":
                    all_running = False

                result[c_name] = ContainerHealth(
                    name=c_name,
                    status=status,
                    health_status=health_status,
                    restart_count=restart_count,
                )

            return DockerMetrics(
                available=True,
                containers=result,
                all_running=all_running if result else True,
            )
        except Exception as exc:
            return DockerMetrics(
                available=False,
                error_message=f"Failed to query Docker containers: {exc}",
            )

    async def fetch_metrics(self) -> DockerMetrics:
        """Asynchronously executes container queries in a worker thread."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._sync_get_containers)


class TelemetryCollector:
    """
    Unified Telemetry Ingestion Engine combining Locust and Docker monitors.
    """

    def __init__(
        self,
        locust_client: Optional[LocustClient] = None,
        docker_monitor: Optional[DockerMonitor] = None,
        locust_url: Optional[str] = None,
        docker_host: Optional[str] = None,
        sandbox_project: Optional[str] = None,
    ):
        self.locust_client = locust_client or LocustClient(base_url=locust_url)
        self.docker_monitor = docker_monitor or DockerMonitor(
            project_name=sandbox_project,
            docker_host=docker_host,
        )

    async def collect_snapshot(self) -> TelemetrySnapshot:
        """
        Polls Locust and Docker concurrently and returns a unified snapshot.
        """
        now = datetime.now(timezone.utc)
        locust_task = asyncio.create_task(self.locust_client.fetch_stats())
        docker_task = asyncio.create_task(self.docker_monitor.fetch_metrics())

        locust_metrics, docker_metrics = await asyncio.gather(
            locust_task, docker_task, return_exceptions=True
        )

        if isinstance(locust_metrics, Exception):
            locust_metrics = LocustMetrics(
                available=False,
                error_message=f"Locust gather exception: {locust_metrics}",
            )

        if isinstance(docker_metrics, Exception):
            docker_metrics = DockerMetrics(
                available=False,
                error_message=f"Docker gather exception: {docker_metrics}",
            )

        is_healthy = True
        err_msg = None

        if not locust_metrics.available:
            is_healthy = False
            err_msg = locust_metrics.error_message
        elif locust_metrics.error_rate is not None and locust_metrics.error_rate > 0.05:
            is_healthy = False
            err_msg = f"Error rate exceeded 5%: {locust_metrics.error_rate:.2%}"

        if docker_metrics.available and not docker_metrics.all_running:
            is_healthy = False
            unhealthy_containers = [
                name for name, c in docker_metrics.containers.items() if c.status != "running"
            ]
            err_msg = (
                f"{err_msg}; " if err_msg else ""
            ) + f"Containers not running: {', '.join(unhealthy_containers)}"

        return TelemetrySnapshot(
            timestamp=now,
            locust=locust_metrics,
            docker=docker_metrics,
            error_rate=locust_metrics.error_rate,
            p95_latency=locust_metrics.p95_latency,
            is_healthy=is_healthy,
            error_message=err_msg,
        )

    @staticmethod
    def persist_snapshot(snapshot: TelemetrySnapshot, db_session, is_baseline: bool = False) -> None:
        """
        Persists a telemetry snapshot into SQLite MetricsSnapshot if metrics are available.
        """
        if snapshot.error_rate is None or snapshot.p95_latency is None:
            return

        try:
            from models import MetricsSnapshot

            record = MetricsSnapshot(
                timestamp=snapshot.timestamp,
                error_rate=float(snapshot.error_rate),
                p95_latency=float(snapshot.p95_latency),
                is_baseline=is_baseline,
                rmq_depth=getattr(snapshot, 'rmq_depth', 0)
            )
            db_session.add(record)
            db_session.commit()
        except Exception as exc:
            db_session.rollback()
            logger.error("Failed to persist snapshot to SQLite: %s", exc)
