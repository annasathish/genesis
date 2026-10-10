import os
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROLLER_DIR = os.path.join(REPO_ROOT, "controller")
if CONTROLLER_DIR not in sys.path:
    sys.path.insert(0, CONTROLLER_DIR)

from heartbeat import HeartbeatManager


class TestHeartbeatManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.hb_file = os.path.join(self.temp_dir.name, "test_heartbeat.json")
        self.manager = HeartbeatManager(self.hb_file)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_read_missing_heartbeat(self):
        exists, age, data = self.manager.read_heartbeat()
        self.assertFalse(exists)
        self.assertEqual(age, float("inf"))

    def test_write_and_read_fresh_heartbeat(self):
        write_time = self.manager.write_heartbeat(status="ok", extra={"worker": "fastapi"})
        self.assertTrue(os.path.exists(self.hb_file))

        exists, age, data = self.manager.read_heartbeat()
        self.assertTrue(exists)
        self.assertLess(age, 2.0)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["metadata"]["worker"], "fastapi")
        self.assertAlmostEqual(data["epoch"], write_time, places=1)

    def test_stale_heartbeat_calculation(self):
        # Write an artificial heartbeat with old epoch
        old_epoch = time.time() - 35.0
        self.manager.write_heartbeat()

        # Manually alter file to simulate 35s age
        import json
        with open(self.hb_file, "r") as f:
            d = json.load(f)
        d["epoch"] = old_epoch
        with open(self.hb_file, "w") as f:
            json.dump(d, f)

        exists, age, data = self.manager.read_heartbeat()
        self.assertTrue(exists)
        self.assertGreaterEqual(age, 34.0)


if __name__ == "__main__":
    unittest.main()
