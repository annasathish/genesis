import asyncio
from datetime import datetime, timezone
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROLLER_DIR = os.path.join(REPO_ROOT, "controller")
if CONTROLLER_DIR not in sys.path:
    sys.path.insert(0, CONTROLLER_DIR)

from telemetry import (
    ContainerHealth,
    DockerMetrics,
    DockerMonitor,
    LocustClient,
    LocustMetrics,
    TelemetryCollector,
    TelemetrySnapshot,
)


class TestLocustClient(unittest.TestCase):
    def setUp(self):
        self.client = LocustClient(base_url="http://test-locust:8089", timeout_seconds=1.0)

    def test_parse_locust_standard_response(self):
        sample_data = {
            "state": "running",
            "total_rps": 45.5,
            "fail_ratio": 0.02,
            "stats": [
                {
                    "name": "/cart",
                    "num_requests": 200,
                    "num_failures": 4,
                    "response_time_percentile_0.95": 55.0,
                    "current_rps": 15.0,
                },
                {
                    "name": "Aggregated",
                    "num_requests": 1000,
                    "num_failures": 20,
                    "current_response_time_percentile_95": 62.5,
                    "current_rps": 45.5,
                },
            ],
        }

        metrics = self.client._parse_locust_data(sample_data)
        self.assertTrue(metrics.available)
        self.assertEqual(metrics.state, "running")
        self.assertAlmostEqual(metrics.error_rate, 0.02)
        self.assertEqual(metrics.p95_latency, 62.5)
        self.assertEqual(metrics.current_rps, 45.5)
        self.assertEqual(metrics.num_requests, 1000)
        self.assertEqual(metrics.num_failures, 20)

    def test_extract_p95_from_histogram(self):
        agg_row = {
            "name": "Aggregated",
            "num_requests": 100,
            "num_failures": 0,
            # 100 total requests: 50 at 10ms, 45 at 20ms (95 cumulative), 5 at 100ms
            "response_times": {"10": 50, "20": 45, "100": 5},
        }
        p95 = self.client._extract_p95(agg_row)
        self.assertEqual(p95, 20.0)

    def test_extract_p95_fallback_avg(self):
        agg_row = {
            "name": "Aggregated",
            "num_requests": 10,
            "num_failures": 0,
            "avg_response_time": 33.4,
        }
        p95 = self.client._extract_p95(agg_row)
        self.assertEqual(p95, 33.4)

    def test_empty_stats_list(self):
        sample_data = {"state": "ready", "total_rps": 0.0, "stats": []}
        metrics = self.client._parse_locust_data(sample_data)
        self.assertTrue(metrics.available)
        self.assertEqual(metrics.num_requests, 0)
        self.assertEqual(metrics.error_rate, 0.0)
        self.assertEqual(metrics.p95_latency, 0.0)

    def test_fetch_stats_network_failure(self):
        async def run_test():
            with patch("httpx.AsyncClient.get", side_effect=Exception("Connection refused")):
                metrics = await self.client.fetch_stats()
                self.assertFalse(metrics.available)
                self.assertIsNone(metrics.error_rate)
                self.assertIsNone(metrics.p95_latency)
                self.assertIn("Connection refused", metrics.error_message)

        asyncio.run(run_test())

    def test_fetch_stats_http_500(self):
        async def run_test():
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            with patch("httpx.AsyncClient.get", return_value=mock_resp):
                metrics = await self.client.fetch_stats()
                self.assertFalse(metrics.available)
                self.assertIn("HTTP 500", metrics.error_message)

        asyncio.run(run_test())


class TestDockerMonitor(unittest.TestCase):
    def test_sync_get_containers_when_healthy(self):
        monitor = DockerMonitor(project_name="robot-shop")

        mock_container1 = MagicMock()
        mock_container1.name = "robot-shop-web-1"
        mock_container1.status = "running"
        mock_container1.labels = {"com.docker.compose.project": "robot-shop"}
        mock_container1.attrs = {"State": {"Health": {"Status": "healthy"}}, "RestartCount": 0}

        mock_container2 = MagicMock()
        mock_container2.name = "robot-shop-cart-1"
        mock_container2.status = "running"
        mock_container2.labels = {"com.docker.compose.project": "robot-shop"}
        mock_container2.attrs = {"State": {}, "RestartCount": 1}

        mock_docker_client = MagicMock()
        mock_docker_client.containers.list.return_value = [mock_container1, mock_container2]

        with patch("telemetry.docker") as mock_docker:
            mock_docker.from_env.return_value = mock_docker_client
            metrics = monitor._sync_get_containers()

            self.assertTrue(metrics.available)
            self.assertTrue(metrics.all_running)
            self.assertEqual(len(metrics.containers), 2)
            self.assertEqual(metrics.containers["robot-shop-web-1"].health_status, "healthy")
            self.assertEqual(metrics.containers["robot-shop-cart-1"].restart_count, 1)

    def test_sync_get_containers_when_container_crashed(self):
        monitor = DockerMonitor(project_name="robot-shop")

        mock_container = MagicMock()
        mock_container.name = "robot-shop-ratings-1"
        mock_container.status = "exited"
        mock_container.labels = {"com.docker.compose.project": "robot-shop"}
        mock_container.attrs = {"State": {}, "RestartCount": 0}

        mock_docker_client = MagicMock()
        mock_docker_client.containers.list.return_value = [mock_container]

        with patch("telemetry.docker") as mock_docker:
            mock_docker.from_env.return_value = mock_docker_client
            metrics = monitor._sync_get_containers()

            self.assertTrue(metrics.available)
            self.assertFalse(metrics.all_running)
            self.assertEqual(metrics.containers["robot-shop-ratings-1"].status, "exited")

    def test_sync_get_containers_when_docker_daemon_down(self):
        monitor = DockerMonitor()
        with patch("telemetry.docker") as mock_docker:
            mock_docker.from_env.side_effect = Exception("Cannot connect to Docker daemon")
            metrics = monitor._sync_get_containers()
            self.assertFalse(metrics.available)
            self.assertIn("Docker daemon unavailable", metrics.error_message)


class TestTelemetryCollector(unittest.TestCase):
    def test_collect_snapshot_healthy(self):
        async def run_test():
            mock_locust = MagicMock()
            mock_locust.fetch_stats = AsyncMock(
                return_value=LocustMetrics(
                    available=True,
                    state="running",
                    error_rate=0.01,
                    p95_latency=42.0,
                    current_rps=25.0,
                    num_requests=500,
                    num_failures=5,
                )
            )

            mock_docker = MagicMock()
            mock_docker.fetch_metrics = AsyncMock(
                return_value=DockerMetrics(
                    available=True,
                    containers={"c1": ContainerHealth(name="c1", status="running")},
                    all_running=True,
                )
            )

            collector = TelemetryCollector(
                locust_client=mock_locust,
                docker_monitor=mock_docker,
            )

            snapshot = await collector.collect_snapshot()
            self.assertTrue(snapshot.is_healthy)
            self.assertEqual(snapshot.error_rate, 0.01)
            self.assertEqual(snapshot.p95_latency, 42.0)
            self.assertTrue(snapshot.docker.all_running)

        asyncio.run(run_test())

    def test_collect_snapshot_locust_offline_unhealthy(self):
        async def run_test():
            mock_locust = MagicMock()
            mock_locust.fetch_stats = AsyncMock(
                return_value=LocustMetrics(
                    available=False,
                    error_message="Locust offline",
                )
            )

            mock_docker = MagicMock()
            mock_docker.fetch_metrics = AsyncMock(
                return_value=DockerMetrics(available=True, all_running=True)
            )

            collector = TelemetryCollector(
                locust_client=mock_locust,
                docker_monitor=mock_docker,
            )

            snapshot = await collector.collect_snapshot()
            self.assertFalse(snapshot.is_healthy)
            self.assertIsNone(snapshot.error_rate)
            self.assertIsNone(snapshot.p95_latency)
            self.assertIn("Locust offline", snapshot.error_message)

        asyncio.run(run_test())

    def test_collect_snapshot_error_rate_spike_unhealthy(self):
        async def run_test():
            mock_locust = MagicMock()
            mock_locust.fetch_stats = AsyncMock(
                return_value=LocustMetrics(
                    available=True,
                    error_rate=0.08,  # 8% > 5%
                    p95_latency=120.0,
                    current_rps=10.0,
                    num_requests=100,
                    num_failures=8,
                )
            )

            mock_docker = MagicMock()
            mock_docker.fetch_metrics = AsyncMock(
                return_value=DockerMetrics(available=True, all_running=True)
            )

            collector = TelemetryCollector(
                locust_client=mock_locust,
                docker_monitor=mock_docker,
            )

            snapshot = await collector.collect_snapshot()
            self.assertFalse(snapshot.is_healthy)
            self.assertIn("Error rate exceeded 5%", snapshot.error_message)

        asyncio.run(run_test())

    def test_persist_snapshot_with_db(self):
        snapshot = TelemetrySnapshot(
            timestamp=datetime.now(timezone.utc),
            locust=LocustMetrics(available=True, error_rate=0.01, p95_latency=35.0),
            docker=DockerMetrics(available=True, all_running=True),
            error_rate=0.01,
            p95_latency=35.0,
            is_healthy=True,
        )

        mock_session = MagicMock()
        mock_model = MagicMock()

        with patch.dict("sys.modules", {"models": MagicMock(MetricsSnapshot=mock_model)}):
            TelemetryCollector.persist_snapshot(snapshot, mock_session, is_baseline=True)
            self.assertTrue(mock_session.add.called)
            self.assertTrue(mock_session.commit.called)

    def test_persist_snapshot_skips_when_metrics_none(self):
        snapshot = TelemetrySnapshot(
            timestamp=datetime.now(timezone.utc),
            locust=LocustMetrics(available=False),
            docker=DockerMetrics(available=True, all_running=True),
            error_rate=None,
            p95_latency=None,
            is_healthy=False,
        )

        mock_session = MagicMock()
        TelemetryCollector.persist_snapshot(snapshot, mock_session, is_baseline=True)
        self.assertFalse(mock_session.add.called)

    def test_snapshot_to_dict(self):
        now = datetime.now(timezone.utc)
        snapshot = TelemetrySnapshot(
            timestamp=now,
            locust=LocustMetrics(available=True, state="running", error_rate=0.02, p95_latency=40.0),
            docker=DockerMetrics(available=True, all_running=True),
            error_rate=0.02,
            p95_latency=40.0,
            is_healthy=True,
        )
        d = snapshot.to_dict()
        self.assertEqual(d["timestamp"], now.isoformat())
        self.assertTrue(d["is_healthy"])
        self.assertEqual(d["error_rate"], 0.02)
        self.assertEqual(d["p95_latency"], 40.0)
        self.assertTrue(d["locust"]["available"])
        self.assertTrue(d["docker"]["available"])


if __name__ == "__main__":
    unittest.main()
