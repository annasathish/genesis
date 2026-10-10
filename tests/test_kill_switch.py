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
        # Mock Docker client with containers:
        # 1: stopped verified sandbox container (robot-shop-cart)
        # 2: running verified sandbox container (robot-shop-web)
        # 3: stopped personal/host container (personal-blog-db) -> MUST NOT BE TOUCHED!
        # 4: stopped container with matching project but MISSING service label -> MUST NOT BE TOUCHED!
        # 5: stopped unlabeled container with misleading prefix -> MUST NOT BE TOUCHED!
        # 6: stopped container with matching project but UNAPPROVED service -> MUST NOT BE TOUCHED!
        mock_c1 = MagicMock()
        mock_c1.name = "robot-shop-cart-1"
        mock_c1.status = "exited"
        mock_c1.labels = {
            "com.docker.compose.project": "robot-shop",
            "com.docker.compose.service": "cart",
        }

        mock_c2 = MagicMock()
        mock_c2.name = "robot-shop-web-1"
        mock_c2.status = "running"
        mock_c2.labels = {
            "com.docker.compose.project": "robot-shop",
            "com.docker.compose.service": "web",
        }

        mock_c3 = MagicMock()
        mock_c3.name = "personal-blog-db"
        mock_c3.status = "exited"
        mock_c3.labels = {"com.docker.compose.project": "personal-app"}

        mock_c4 = MagicMock()
        mock_c4.name = "robot-shop-unlabeled-svc"
        mock_c4.status = "exited"
        mock_c4.labels = {"com.docker.compose.project": "robot-shop"}

        mock_c5 = MagicMock()
        mock_c5.name = "robot-shop-cart-misleading"
        mock_c5.status = "exited"
        mock_c5.labels = {}

        mock_c6 = MagicMock()
        mock_c6.name = "robot-shop-tool"
        mock_c6.status = "exited"
        mock_c6.labels = {
            "com.docker.compose.project": "robot-shop",
            "com.docker.compose.service": "unapproved-debugger",
        }

        mock_client = MagicMock()
        mock_client.containers.list.return_value = [
            mock_c1,
            mock_c2,
            mock_c3,
            mock_c4,
            mock_c5,
            mock_c6,
        ]

        with patch("kill_switch.docker") as mock_docker:
            mock_docker.from_env.return_value = mock_client
            restarted = self.ks._sync_revive_sandbox_containers()

            # Assert only verified stopped sandbox container was started
            self.assertEqual(restarted, ["robot-shop-cart-1"])
            self.assertTrue(mock_c1.start.called)
            self.assertFalse(mock_c2.start.called)
            self.assertFalse(mock_c3.start.called)
            self.assertFalse(mock_c4.start.called)
            self.assertFalse(mock_c5.start.called)
            self.assertFalse(mock_c6.start.called)

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

    def test_lazy_reload_baseline_when_created_after_startup(self):
        """
        Regression test: Verify KillSwitch reloads baseline.json if it was created
        or modified AFTER the controller started up.
        """
        late_baseline_path = os.path.join(self.temp_dir.name, "late_baseline.json")
        ks_late = KillSwitch(
            baseline_path=late_baseline_path,
            db_path=self.db_file,
        )

        # 1. At startup before baseline is created
        self.assertIsNone(ks_late.baseline_bands)
        err_def, p95_def = ks_late.get_thresholds()
        self.assertEqual(err_def, 0.05)
        self.assertEqual(p95_def, 500.0)

        # 2. Baseline is created later by make baseline
        time.sleep(0.02)
        late_data = {
            "p95_latency_ms": {"mean": 60.0},
            "error_rate": {"mean": 0.005},
            "kill_switch_thresholds": {
                "max_error_rate": 0.07,
                "p95_latency_threshold_ms": 300.0,
            },
        }
        with open(late_baseline_path, "w", encoding="utf-8") as f:
            json.dump(late_data, f)

        # 3. Next call to get_thresholds must automatically load it
        err_loaded, p95_loaded = ks_late.get_thresholds()
        self.assertIsNotNone(ks_late.baseline_bands)
        self.assertEqual(err_loaded, 0.07)
        self.assertEqual(p95_loaded, 300.0)

    def test_container_scoping_rejects_substring_matches(self):
        """
        Regression test: Verify that containers require strict verified sandbox ownership:
        - Mismatched compose project labels are rejected immediately.
        - Missing or unapproved service labels under matching project are rejected.
        - Unlabeled containers are never approved by loose names or prefixes.
        - Explicit container ID allowlists are verified for legacy unlabeled containers.
        """
        from kill_switch import is_approved_sandbox_container

        allowed = set(self.ks.allowed_services)

        # 1. Missing service labels under matching project
        missing_service = [
            ("robot-shop-cart-1", {"com.docker.compose.project": "robot-shop"}),
            ("robot-shop-web", {"com.docker.compose.project": "robot-shop", "com.docker.compose.service": ""}),
        ]
        for name, labels in missing_service:
            self.assertFalse(
                is_approved_sandbox_container(name, labels, "robot-shop", allowed),
                f"Container {name} with missing/empty service label must be rejected",
            )

        # 2. Wrong project labels (even with valid service name)
        wrong_project = [
            ("robot-shop-cart-1", {"com.docker.compose.project": "different-project", "com.docker.compose.service": "cart"}),
            ("cart", {"com.docker.compose.project": "foreign-project", "com.docker.compose.service": "cart"}),
            ("web", {"com.docker.compose.project": "other-app", "com.docker.compose.service": "web"}),
        ]
        for name, labels in wrong_project:
            self.assertFalse(
                is_approved_sandbox_container(name, labels, "robot-shop", allowed),
                f"Container {name} with wrong project label must be rejected",
            )

        # 3. Matching project but unapproved service label
        unapproved_service = [
            ("robot-shop-tool", {"com.docker.compose.project": "robot-shop", "com.docker.compose.service": "unapproved-debugger"}),
            ("robot-shop-admin", {"com.docker.compose.project": "robot-shop", "com.docker.compose.service": "root-shell"}),
        ]
        for name, labels in unapproved_service:
            self.assertFalse(
                is_approved_sandbox_container(name, labels, "robot-shop", allowed),
                f"Container {name} with unapproved service label must be rejected",
            )

        # 4. Misleading names / prefixes on unlabeled containers
        unlabeled_misleading = [
            ("personal-web", {}),
            ("mysql-production-db", {}),
            ("user-authentication-service", {}),
            ("analytics-redis-cache", {}),
            ("unrelated-ratings-api", {}),
            ("personal-cart", {}),
            ("robot-shop-cart-1", {}),
            ("robot-shop_ratings_1", {}),
            ("robot-shop-web", {}),
            ("cart", {}),
            ("mysql", {}),
            ("redis", {}),
        ]
        for name, labels in unlabeled_misleading:
            self.assertFalse(
                is_approved_sandbox_container(name, labels, "robot-shop", allowed),
                f"Unlabeled container {name} must NOT be recognized solely by name/prefix",
            )

        # 5. Legitimate Compose-managed sandbox containers
        approved = [
            ("robot-shop-cart-1", {"com.docker.compose.project": "robot-shop", "com.docker.compose.service": "cart"}),
            ("robot-shop_ratings_1", {"com.docker.compose.project": "robot-shop", "com.docker.compose.service": "ratings"}),
            ("robot-shop-web", {"com.docker.compose.project": "robot-shop", "com.docker.compose.service": "web"}),
            ("custom-named", {"com.docker.compose.project": "robot-shop", "com.docker.compose.service": "cart"}),
        ]
        for name, labels in approved:
            self.assertTrue(
                is_approved_sandbox_container(name, labels, "robot-shop", allowed),
                f"Valid sandbox container {name} SHOULD be recognized as approved",
            )

        # 6. Legacy unlabeled container with explicit ID allowlist
        allowed_ids = {"c123456789012345"}
        self.assertTrue(
            is_approved_sandbox_container(
                "legacy-cart",
                {},
                "robot-shop",
                allowed,
                container_id="c123456789012345",
                allowed_container_ids=allowed_ids,
            ),
            "Unlabeled container with matching container_id in allowlist should be approved",
        )
        self.assertTrue(
            is_approved_sandbox_container(
                "legacy-cart",
                {},
                "robot-shop",
                allowed,
                container_id="c123456789012345",
                allowed_container_ids={"c12345678901"},  # 12-char prefix match
            ),
            "Unlabeled container with matching 12-char container_id in allowlist should be approved",
        )
        self.assertFalse(
            is_approved_sandbox_container(
                "legacy-cart",
                {},
                "robot-shop",
                allowed,
                container_id="unapproved-id-999",
                allowed_container_ids=allowed_ids,
            ),
            "Unlabeled container with unapproved container_id must be rejected",
        )


if __name__ == "__main__":
    unittest.main()
