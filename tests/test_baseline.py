import asyncio
from datetime import datetime, timezone
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROLLER_DIR = os.path.join(REPO_ROOT, "controller")
if CONTROLLER_DIR not in sys.path:
    sys.path.insert(0, CONTROLLER_DIR)

from baseline import BaselineCollector, compute_bands
from telemetry import (
    DockerMetrics,
    LocustMetrics,
    TelemetryCollector,
    TelemetrySnapshot,
)


class TestComputeBands(unittest.TestCase):
    def test_compute_bands_typical_data(self):
        # 5 samples: mean = 50.0, variance = 10.0, std = sqrt(10) ≈ 3.162277
        samples = [46.0, 48.0, 50.0, 52.0, 54.0]
        bands = compute_bands(samples)

        self.assertEqual(bands["sample_count"], 5)
        self.assertAlmostEqual(bands["mean"], 50.0, places=4)
        self.assertAlmostEqual(bands["std"], 3.1623, places=3)

        # 3 * std = 3 * 3.1623 ≈ 9.4868
        # 0.20 * mean = 10.0
        # band_margin = max(9.4868, 10.0) = 10.0
        self.assertAlmostEqual(bands["band_margin"], 10.0, places=4)
        self.assertAlmostEqual(bands["upper_bound"], 60.0, places=4)
        self.assertAlmostEqual(bands["lower_bound"], 40.0, places=4)

    def test_compute_bands_high_variance(self):
        # Samples with high variance where 3*std > 0.20*mean
        samples = [10.0, 100.0, 10.0, 100.0]
        bands = compute_bands(samples)

        # mean = 55.0. 0.20 * mean = 11.0
        # std is ~51.96, 3 * std = ~155.88 > 11.0
        self.assertGreater(bands["band_margin"], 11.0)
        self.assertAlmostEqual(bands["upper_bound"], bands["mean"] + bands["band_margin"])
        # lower bound clamped at 0.0
        self.assertEqual(bands["lower_bound"], 0.0)

    def test_compute_bands_single_sample(self):
        samples = [42.0]
        bands = compute_bands(samples)

        self.assertEqual(bands["sample_count"], 1)
        self.assertEqual(bands["mean"], 42.0)
        self.assertEqual(bands["std"], 0.0)
        # 0.20 * 42 = 8.4
        self.assertEqual(bands["band_margin"], 8.4)
        self.assertEqual(bands["upper_bound"], 50.4)
        self.assertEqual(bands["lower_bound"], 33.6)

    def test_compute_bands_empty_raises(self):
        with self.assertRaises(ValueError):
            compute_bands([])


class TestBaselineCollector(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.output_json = os.path.join(self.temp_dir.name, "test_baseline.json")
        self.output_db = os.path.join(self.temp_dir.name, "test_chaos.db")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_run_baseline_success(self):
        async def run_test():
            # Mock telemetry collector returning valid steady-state samples
            mock_collector = MagicMock(spec=TelemetryCollector)
            counter = [0]

            async def mock_snapshot():
                counter[0] += 1
                return TelemetrySnapshot(
                    timestamp=datetime.now(timezone.utc),
                    locust=LocustMetrics(
                        available=True,
                        state="running",
                        error_rate=0.002,
                        p95_latency=45.0 + counter[0],
                        current_rps=20.0,
                        num_requests=100 * counter[0],
                        num_failures=1,
                    ),
                    docker=DockerMetrics(available=True, all_running=True),
                    error_rate=0.002,
                    p95_latency=45.0 + counter[0],
                    is_healthy=True,
                )

            mock_collector.collect_snapshot = AsyncMock(side_effect=mock_snapshot)
            mock_collector.persist_snapshot = MagicMock()

            # Short collector run: 0.3s duration, 0.05s interval (~6 samples)
            collector = BaselineCollector(
                duration_seconds=0.3,
                interval_seconds=0.05,
                output_file=self.output_json,
                db_path=self.output_db,
                telemetry_collector=mock_collector,
            )

            result = await collector.run()

            # Verify return dictionary structure
            self.assertIn("p95_latency_ms", result)
            self.assertIn("error_rate", result)
            self.assertIn("kill_switch_thresholds", result)
            self.assertGreaterEqual(result["sample_count"], 3)

            # Verify Kill Switch limits
            thresholds = result["kill_switch_thresholds"]
            self.assertAlmostEqual(thresholds["max_error_rate"], 0.05, places=2)
            self.assertAlmostEqual(thresholds["p95_multiplier"], 5.0)
            self.assertEqual(thresholds["consecutive_breach_windows"], 2)
            self.assertEqual(thresholds["blind_guardrail_timeout_sec"], 15)

            # Verify baseline.json was saved on disk
            self.assertTrue(os.path.exists(self.output_json))
            with open(self.output_json, "r", encoding="utf-8") as f:
                saved_data = json.load(f)

            self.assertEqual(saved_data["sample_count"], result["sample_count"])
            self.assertEqual(
                saved_data["p95_latency_ms"]["mean"], result["p95_latency_ms"]["mean"]
            )

        asyncio.run(run_test())

    def test_run_baseline_fails_when_locust_unavailable(self):
        async def run_test():
            # Mock telemetry collector where Locust is offline
            mock_collector = MagicMock(spec=TelemetryCollector)
            mock_collector.collect_snapshot = AsyncMock(
                return_value=TelemetrySnapshot(
                    timestamp=datetime.now(timezone.utc),
                    locust=LocustMetrics(available=False, error_message="Connection refused"),
                    docker=DockerMetrics(available=True, all_running=True),
                    error_rate=None,
                    p95_latency=None,
                    is_healthy=False,
                )
            )
            mock_collector.persist_snapshot = MagicMock()

            collector = BaselineCollector(
                duration_seconds=0.1,
                interval_seconds=0.05,
                output_file=self.output_json,
                db_path=self.output_db,
                telemetry_collector=mock_collector,
            )

            # Must raise RuntimeError and not fabricate fake data
            with self.assertRaises(RuntimeError) as ctx:
                await collector.run()

            self.assertIn("Locust was unreachable", str(ctx.exception))
            self.assertFalse(os.path.exists(self.output_json))

        asyncio.run(run_test())

    def test_request_shutdown_stops_early(self):
        async def run_test():
            mock_collector = MagicMock(spec=TelemetryCollector)
            mock_collector.collect_snapshot = AsyncMock(
                return_value=TelemetrySnapshot(
                    timestamp=datetime.now(timezone.utc),
                    locust=LocustMetrics(
                        available=True,
                        error_rate=0.01,
                        p95_latency=50.0,
                    ),
                    docker=DockerMetrics(available=True, all_running=True),
                    error_rate=0.01,
                    p95_latency=50.0,
                    is_healthy=True,
                )
            )
            mock_collector.persist_snapshot = MagicMock()

            # Configure for 100 seconds
            collector = BaselineCollector(
                duration_seconds=100.0,
                interval_seconds=0.05,
                output_file=self.output_json,
                db_path=self.output_db,
                telemetry_collector=mock_collector,
            )

            # Trigger shutdown after collecting 2 samples
            async def shutdown_trigger():
                await asyncio.sleep(0.12)
                collector.request_shutdown()

            asyncio.create_task(shutdown_trigger())
            result = await collector.run()

            self.assertGreaterEqual(result["sample_count"], 1)
            self.assertLess(result["sample_count"], 10)
            self.assertTrue(os.path.exists(self.output_json))

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
