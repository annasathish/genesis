import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROLLER_DIR = os.path.join(REPO_ROOT, "controller")
if CONTROLLER_DIR not in sys.path:
    sys.path.insert(0, CONTROLLER_DIR)

from watchdog import DeadManWatchdog


class TestDeadManWatchdog(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.hb_file = os.path.join(self.temp_dir.name, "watchdog_hb.json")
        self.db_file = os.path.join(self.temp_dir.name, "chaos_test.db")

        self.watchdog = DeadManWatchdog(
            heartbeat_file=self.hb_file,
            timeout_seconds=20.0,
            check_interval_seconds=5.0,
            db_path=self.db_file,
            sandbox_project="robot-shop",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_fresh_heartbeat_healthy(self):
        # Write fresh heartbeat
        with open(self.hb_file, "w") as f:
            json.dump({"epoch": time.time(), "status": "ok"}, f)

        is_alive, age, _ = self.watchdog.is_controller_healthy()
        self.assertTrue(is_alive)
        self.assertLess(age, 2.0)
        self.assertTrue(self.watchdog.check_once())

    def test_stale_heartbeat_triggers_emergency_rollback(self):
        # Write heartbeat that is 25s old (> 20s timeout)
        stale_epoch = time.time() - 25.0
        with open(self.hb_file, "w") as f:
            json.dump({"epoch": stale_epoch, "status": "ok"}, f)

        is_alive, age, _ = self.watchdog.is_controller_healthy()
        self.assertFalse(is_alive)
        self.assertGreaterEqual(age, 24.0)

        with patch.object(self.watchdog, "trigger_emergency_rollback") as mock_rollback:
            mock_rollback.return_value = {"status": "recovered"}
            alive = self.watchdog.check_once()
            self.assertFalse(alive)
            self.assertTrue(mock_rollback.called)

    def test_missing_heartbeat_file_triggers_rollback(self):
        if os.path.exists(self.hb_file):
            os.remove(self.hb_file)

        is_alive, age, data = self.watchdog.is_controller_healthy()
        self.assertFalse(is_alive)
        self.assertIn("error", data)

    def test_container_revival_strictly_limited_to_sandbox(self):
        # 3 containers:
        # c1: stopped robot-shop container
        # c2: running robot-shop container
        # c3: stopped unrelated container (e.g. system-metrics) -> MUST NOT BE STARTED
        mock_c1 = MagicMock()
        mock_c1.name = "robot-shop-shipping-1"
        mock_c1.status = "exited"
        mock_c1.labels = {"com.docker.compose.project": "robot-shop"}

        mock_c2 = MagicMock()
        mock_c2.name = "robot-shop-web-1"
        mock_c2.status = "running"
        mock_c2.labels = {"com.docker.compose.project": "robot-shop"}

        mock_c3 = MagicMock()
        mock_c3.name = "system-monitoring-prometheus"
        mock_c3.status = "exited"
        mock_c3.labels = {"com.docker.compose.project": "system-infra"}

        mock_client = MagicMock()
        mock_client.containers.list.return_value = [mock_c1, mock_c2, mock_c3]

        with patch.object(self.watchdog, "_get_docker_client", return_value=mock_client):
            restarted = self.watchdog._revive_sandbox_containers()

            self.assertEqual(restarted, ["robot-shop-shipping-1"])
            self.assertTrue(mock_c1.start.called)
            self.assertFalse(mock_c2.start.called)
            self.assertFalse(mock_c3.start.called)

    def test_purge_toxiproxy_toxics(self):
        mock_client = MagicMock()

        mock_proxies_resp = MagicMock(status_code=200)
        mock_proxies_resp.json.return_value = {"shipping_proxy": {}}

        mock_toxics_resp = MagicMock(status_code=200)
        mock_toxics_resp.json.return_value = [{"name": "jitter_toxic"}]

        mock_del_resp = MagicMock(status_code=200)

        mock_client.get.side_effect = [mock_proxies_resp, mock_toxics_resp]
        mock_client.delete.return_value = mock_del_resp

        with patch("httpx.Client", return_value=mock_client):
            mock_client.__enter__.return_value = mock_client
            mock_client.__exit__.return_value = None

            purged = self.watchdog._purge_toxiproxy_toxics()
            self.assertEqual(purged, 1)
            mock_client.delete.assert_called_with(
                f"{self.watchdog.toxiproxy_url}/proxies/shipping_proxy/toxics/jitter_toxic"
            )

    def test_abort_orphaned_intents_in_database(self):
        mock_intent = MagicMock()
        mock_intent.id = 10
        mock_intent.status = "running"

        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_session.query.return_value = mock_query
        mock_query.filter.return_value.all.return_value = [mock_intent]
        mock_query.filter.return_value.first.return_value = None

        with open(self.db_file, "w") as f:
            f.write("")  # Create empty db file so path exists

        with patch.dict(
            "sys.modules",
            {
                "models": MagicMock(
                    Base=MagicMock(),
                    ExperimentIntent=MagicMock(),
                    ExperimentResult=MagicMock(),
                ),
                "sqlalchemy": MagicMock(create_engine=MagicMock()),
                "sqlalchemy.orm": MagicMock(sessionmaker=MagicMock(return_value=MagicMock(return_value=mock_session))),
            },
        ):
            updated = self.watchdog._abort_orphaned_intents()
            self.assertTrue(updated)
            self.assertEqual(mock_intent.status, "aborted")
            self.assertTrue(mock_session.commit.called)


if __name__ == "__main__":
    unittest.main()
