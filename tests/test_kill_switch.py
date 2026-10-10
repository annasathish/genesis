import asyncio
from datetime import datetime, timezone
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROLLER_DIR = os.path.join(REPO_ROOT, "controller")
if CONTROLLER_DIR not in sys.path:
    sys.path.insert(0, CONTROLLER_DIR)

from kill_switch import KillSwitch, KillSwitchState, TripReason
from telemetry import (
    ContainerHealth,
    DockerMetrics,
    LocustMetrics,
    TelemetryCollector,
    TelemetrySnapshot,
)


def make_snapshot(error_rate=0.01, p95=40.0, locust_available=True):
    return TelemetrySnapshot(
        timestamp=datetime.now(timezone.utc),
        locust=LocustMetrics(
            available=locust_available,
            state="running" if locust_available else "unknown",
            error_rate=error_rate if locust_available else None,
            p95_latency=p95 if locust_available else None,
            current_rps=20.0 if locust_available else 0.0,
            error_message=None if locust_available else "Locust unreachable",
        ),
        docker=DockerMetrics(available=True, all_running=True),
        error_rate=error_rate if locust_available else None,
        p95_latency=p95 if locust_available else None,
        is_healthy=locust_available and (error_rate <= 0.05),
    )


class TestKillSwitch(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.baseline_file = os.path.join(self.temp_dir.name, "test_baseline.json")
        self.db_file = os.path.join(self.temp_dir.name, "test_chaos.db")

        # Create sample baseline.json
        baseline_data = {
            "p95_latency_ms": {"mean": 50.0, "upper_bound": 65.0},
            "error_rate": {"mean": 0.005, "upper_bound": 0.01},
            "kill_switch_thresholds": {
                "max_error_rate": 0.05,
                "p95_latency_threshold_ms": 250.0,  # 5 * 50ms
                "blind_guardrail_timeout_sec": 15,
                "consecutive_breach_windows": 2,
            },
        }
        with open(self.baseline_file, "w", encoding="utf-8") as f:
            json.dump(baseline_data, f)

        self.ks = KillSwitch(
            baseline_path=self.baseline_file,
            db_path=self.db_file,
            blind_guardrail_timeout_seconds=15.0,
            consecutive_windows_required=2,
            poll_interval_seconds=5.0,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_thresholds_loaded(self):
        max_err, max_p95 = self.ks.get_thresholds()
        self.assertEqual(max_err, 0.05)
        self.assertEqual(max_p95, 250.0)

    def test_normal_telemetry_no_trip(self):
        snap = make_snapshot(error_rate=0.01, p95=45.0)
        res = self.ks.evaluate_snapshot(snap, has_active_chaos=True)
        self.assertFalse(res.tripped)
        self.assertEqual(res.state, KillSwitchState.ARMED)
        self.assertEqual(self.ks.consecutive_breaches, 0)

    def test_single_window_spike_does_not_trip_filters_false_alarms(self):
        # Window 1: error rate spikes to 8% (> 5%)
        snap1 = make_snapshot(error_rate=0.08, p95=50.0)
        res1 = self.ks.evaluate_snapshot(snap1, has_active_chaos=True)
        self.assertFalse(res1.tripped)
        self.assertEqual(res1.state, KillSwitchState.WARNING_WINDOW_1)
        self.assertEqual(self.ks.consecutive_breaches, 1)

        # Window 2: metrics return to normal (1%)
        snap2 = make_snapshot(error_rate=0.01, p95=45.0)
        res2 = self.ks.evaluate_snapshot(snap2, has_active_chaos=True)
        self.assertFalse(res2.tripped)
        self.assertEqual(res2.state, KillSwitchState.ARMED)
        # Verify false alarm was suppressed and counter reset
        self.assertEqual(self.ks.consecutive_breaches, 0)

    def test_two_consecutive_windows_error_rate_spike_trips(self):
        # Window 1: error rate 7%
        snap1 = make_snapshot(error_rate=0.07, p95=50.0)
        res1 = self.ks.evaluate_snapshot(snap1, has_active_chaos=True)
        self.assertFalse(res1.tripped)
        self.assertEqual(res1.state, KillSwitchState.WARNING_WINDOW_1)

        # Window 2: error rate 9%
        snap2 = make_snapshot(error_rate=0.09, p95=50.0)
        res2 = self.ks.evaluate_snapshot(snap2, has_active_chaos=True)
        self.assertTrue(res2.tripped)
        self.assertEqual(res2.state, KillSwitchState.TRIPPED)
        self.assertIn("error_rate", res2.reason)

    def test_two_consecutive_windows_latency_breach_trips(self):
        # Latency limit is 250ms (5 * 50ms baseline)
        snap1 = make_snapshot(error_rate=0.01, p95=300.0)
        res1 = self.ks.evaluate_snapshot(snap1, has_active_chaos=True)
        self.assertFalse(res1.tripped)
        self.assertEqual(res1.state, KillSwitchState.WARNING_WINDOW_1)

        snap2 = make_snapshot(error_rate=0.01, p95=400.0)
        res2 = self.ks.evaluate_snapshot(snap2, has_active_chaos=True)
        self.assertTrue(res2.tripped)
        self.assertEqual(res2.state, KillSwitchState.TRIPPED)
        self.assertIn("p95", res2.reason)

    def test_blind_guardrail_trips_when_locust_silent_for_15s(self):
        t0 = 1000.0
        # Time t0: normal telemetry
        snap_ok = make_snapshot(locust_available=True)
        self.ks.evaluate_snapshot(snap_ok, now=t0)

        # Time t0 + 5s: Locust goes down (5s gap < 15s)
        snap_down = make_snapshot(locust_available=False)
        res1 = self.ks.evaluate_snapshot(snap_down, now=t0 + 5.0)
        self.assertFalse(res1.tripped)

        # Time t0 + 10s: still down (10s gap < 15s)
        res2 = self.ks.evaluate_snapshot(snap_down, now=t0 + 10.0)
        self.assertFalse(res2.tripped)

        # Time t0 + 15s: 15 seconds reached -> immediate trip!
        res3 = self.ks.evaluate_snapshot(snap_down, now=t0 + 15.0)
        self.assertTrue(res3.tripped)
        self.assertEqual(res3.state, KillSwitchState.TRIPPED)
        self.assertEqual(res3.reason, TripReason.BLIND_GUARDRAIL.value)

    def test_rollback_safety_strictly_scopes_to_sandbox_containers(self):
        # Mock Docker client with 3 containers:
        # 1: stopped sandbox container (robot-shop-cart)
        # 2: running sandbox container (robot-shop-web)
        # 3: stopped personal/host container (personal-blog-db) -> MUST NOT BE TOUCHED!
        mock_c1 = MagicMock()
        mock_c1.name = "robot-shop-cart-1"
        mock_c1.status = "exited"
        mock_c1.labels = {"com.docker.compose.project": "robot-shop"}

        mock_c2 = MagicMock()
        mock_c2.name = "robot-shop-web-1"
        mock_c2.status = "running"
        mock_c2.labels = {"com.docker.compose.project": "robot-shop"}

        mock_c3 = MagicMock()
        mock_c3.name = "personal-blog-db"
        mock_c3.status = "exited"
        mock_c3.labels = {"com.docker.compose.project": "personal-app"}

        mock_client = MagicMock()
        mock_client.containers.list.return_value = [mock_c1, mock_c2, mock_c3]

        with patch("kill_switch.docker") as mock_docker:
            mock_docker.from_env.return_value = mock_client
            restarted = self.ks._sync_revive_sandbox_containers()

            # Assert only stopped sandbox container was started
            self.assertEqual(restarted, ["robot-shop-cart-1"])
            self.assertTrue(mock_c1.start.called)
            self.assertFalse(mock_c2.start.called)
            self.assertFalse(mock_c3.start.called)

    def test_toxiproxy_toxic_purge_on_rollback(self):
        async def run_test():
            mock_client = AsyncMock()

            # GET /proxies returns proxy 'cart'
            mock_proxies_resp = MagicMock(status_code=200)
            mock_proxies_resp.json.return_value = {"cart_proxy": {}}

            # GET /proxies/cart_proxy/toxics returns 1 toxic
            mock_toxics_resp = MagicMock(status_code=200)
            mock_toxics_resp.json.return_value = [{"name": "latency_chaos"}]

            # DELETE returns 200
            mock_del_resp = MagicMock(status_code=200)

            mock_client.get.side_effect = [mock_proxies_resp, mock_toxics_resp]
            mock_client.delete.return_value = mock_del_resp

            with patch("httpx.AsyncClient", return_value=mock_client):
                mock_client.__aenter__.return_value = mock_client
                mock_client.__aexit__.return_value = None

                deleted = await self.ks._clear_all_toxiproxy_toxics()
                self.assertEqual(deleted, 1)
                mock_client.delete.assert_called_with(
                    f"{self.ks.toxiproxy_url}/proxies/cart_proxy/toxics/latency_chaos"
                )

        asyncio.run(run_test())

    def test_database_intent_abort_and_result_logging(self):
        mock_intent = MagicMock()
        mock_intent.id = 42
        mock_intent.status = "running"

        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_session.query.return_value = mock_query
        mock_query.filter.return_value.all.return_value = [mock_intent]
        mock_query.filter.return_value.first.return_value = None

        mock_result_cls = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "models": MagicMock(
                    Base=MagicMock(),
                    ExperimentIntent=MagicMock(),
                    ExperimentResult=mock_result_cls,
                ),
                "sqlalchemy": MagicMock(create_engine=MagicMock()),
                "sqlalchemy.orm": MagicMock(sessionmaker=MagicMock(return_value=MagicMock(return_value=mock_session))),
            },
        ):
            self.ks._update_db_intent_aborted(intent_id=42)

            self.assertEqual(mock_intent.status, "aborted")
            self.assertTrue(mock_session.commit.called)
            self.assertTrue(mock_session.add.called)


if __name__ == "__main__":
    unittest.main()
