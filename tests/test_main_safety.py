import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROLLER_DIR = os.path.join(REPO_ROOT, "controller")
if CONTROLLER_DIR not in sys.path:
    sys.path.insert(0, CONTROLLER_DIR)

import httpx

# Mock external models/engine during testing if necessary
from main import app


class TestMainSafetyEndpoints(unittest.TestCase):
    def test_root_and_m4_endpoints(self):
        async def run_test():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                # 1. Test Root
                resp = await client.get("/")
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn("M4", data.get("roles", []))

                # 2. Test Telemetry Endpoint
                resp_tel = await client.get("/api/v1/telemetry/current")
                self.assertEqual(resp_tel.status_code, 200)
                tel_data = resp_tel.json()
                self.assertIn("is_healthy", tel_data)
                self.assertIn("locust", tel_data)
                self.assertIn("docker", tel_data)

                # 3. Test Safety Status (Pre-flight check for M3)
                resp_safety = await client.get("/api/v1/safety/status")
                self.assertEqual(resp_safety.status_code, 200)
                safety_data = resp_safety.json()
                self.assertIn("is_safe_to_inject", safety_data)
                self.assertIn("kill_switch_state", safety_data)
                self.assertIn("thresholds", safety_data)

                # 4. Test Heartbeat endpoint
                resp_hb = await client.get("/api/v1/safety/heartbeat")
                self.assertEqual(resp_hb.status_code, 200)
                hb_data = resp_hb.json()
                self.assertIn("heartbeat_active", hb_data)

                # 5. Test Baseline Trigger endpoint
                resp_bl = await client.post("/api/v1/baseline?test_mode=true")
                self.assertEqual(resp_bl.status_code, 200)
                bl_data = resp_bl.json()
                self.assertEqual(bl_data["status"], "started")

                # 6. Test Manual Trip endpoint
                resp_trip = await client.post("/api/v1/safety/trip?reason=ManualTest")
                self.assertEqual(resp_trip.status_code, 200)
                trip_data = resp_trip.json()
                self.assertEqual(trip_data["status"], "tripped")

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
