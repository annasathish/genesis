"""
M5 - Integration Tests for the Reporting Module

Validates that the full reporting pipeline works by:
  1. Creating an in-memory SQLite DB with realistic experiment data.
  2. Running the scorer, reporter, and CLI without the FastAPI server.
  3. Verifying outputs (report.json, REPORT.md) are generated correctly.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, ExperimentIntent, ExperimentResult, MetricsSnapshot
from scorer import (
    compute_error_score,
    compute_latency_score,
    compute_recovery_score,
    compute_resilience_score,
)
from reporter import generate_report


class TestScorer(unittest.TestCase):
    """Unit tests for the normalised scoring functions."""

    def test_latency_score_no_degradation(self):
        """p95 stayed at baseline → score = 1.0"""
        self.assertAlmostEqual(compute_latency_score(100, 100), 1.0)

    def test_latency_score_5x_degradation(self):
        """p95 is 5× baseline → ratio=5 → score = 0.0"""
        self.assertAlmostEqual(compute_latency_score(500, 100), 0.0)

    def test_latency_score_3x_degradation(self):
        """p95 is 3× baseline → ratio=3 → score = 0.5"""
        self.assertAlmostEqual(compute_latency_score(300, 100), 0.5)

    def test_latency_score_zero_baseline(self):
        """Zero baseline should return 1.0 (no penalty)."""
        self.assertAlmostEqual(compute_latency_score(500, 0), 1.0)

    def test_error_score_zero_errors(self):
        self.assertAlmostEqual(compute_error_score(0.0), 1.0)

    def test_error_score_at_tolerance(self):
        self.assertAlmostEqual(compute_error_score(0.10, 0.10), 0.0)

    def test_error_score_half_tolerance(self):
        self.assertAlmostEqual(compute_error_score(0.05, 0.10), 0.5)

    def test_error_score_over_tolerance(self):
        """Error rate above tolerance should clamp to 0."""
        self.assertAlmostEqual(compute_error_score(0.20, 0.10), 0.0)

    def test_recovery_score_instant(self):
        self.assertAlmostEqual(compute_recovery_score(0.0), 1.0)

    def test_recovery_score_max(self):
        self.assertAlmostEqual(compute_recovery_score(300.0, 300.0), 0.0)

    def test_recovery_score_none(self):
        self.assertAlmostEqual(compute_recovery_score(None), 0.0)

    def test_composite_bounds(self):
        """Composite score must always be in [0, 1]."""
        result = compute_resilience_score(
            p95_during=10000, p95_baseline=10,
            error_rate=1.0, recovery_time_sec=9999,
            kill_switch_fired=True,
        )
        self.assertGreaterEqual(result["composite_score"], 0.0)
        self.assertLessEqual(result["composite_score"], 1.0)

    def test_composite_perfect(self):
        result = compute_resilience_score(
            p95_during=100, p95_baseline=100,
            error_rate=0.0, recovery_time_sec=0.0,
            kill_switch_fired=False,
        )
        self.assertAlmostEqual(result["composite_score"], 1.0)
        self.assertEqual(result["score_out_of_10"], 10.0)

    def test_kill_switch_not_in_score(self):
        """Kill switch should NOT affect the composite score directly."""
        score_no_ks = compute_resilience_score(
            p95_during=200, p95_baseline=100,
            error_rate=0.03, recovery_time_sec=60,
            kill_switch_fired=False,
        )
        score_ks = compute_resilience_score(
            p95_during=200, p95_baseline=100,
            error_rate=0.03, recovery_time_sec=60,
            kill_switch_fired=True,
        )
        # Same inputs except kill_switch → same composite
        self.assertEqual(
            score_no_ks["composite_score"],
            score_ks["composite_score"],
        )
        # But metadata differs
        self.assertFalse(score_no_ks["kill_switch_fired"])
        self.assertTrue(score_ks["kill_switch_fired"])


class TestReporter(unittest.TestCase):
    """
    Integration test: builds a real SQLite DB in a temp dir, populates it,
    and verifies the reporter can produce report.json + REPORT.md without
    the FastAPI server running.
    """

    def setUp(self):
        """Create an in-memory–like temp SQLite and seed it."""
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_chaos.db")
        self.db_url = f"sqlite:///{self.db_path}"

        engine = create_engine(self.db_url, connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        session = Session()

        now = datetime.now(timezone.utc)

        # Seed baseline metrics (10 snapshots)
        for i in range(10):
            session.add(MetricsSnapshot(
                timestamp=now - timedelta(minutes=20 - i),
                error_rate=0.01,
                p95_latency=100.0,
                is_baseline=True,
            ))

        # Experiment 1: container_crash on cart (bad outcome)
        intent1 = ExperimentIntent(
            fault_type="container_crash",
            target_service="cart",
            fault_params='{}',
            rollback_command="docker start cart",
            status="aborted",
            created_at=now - timedelta(minutes=5),
            expires_at=now + timedelta(minutes=5),
        )
        session.add(intent1)
        session.flush()

        session.add(ExperimentResult(
            intent_id=intent1.id,
            kill_switch_fired=True,
            recovery_time_sec=120.0,
            resilience_score=None,
        ))

        # During-fault metrics for experiment 1
        for i in range(5):
            session.add(MetricsSnapshot(
                timestamp=now - timedelta(minutes=4) + timedelta(seconds=i * 10),
                error_rate=0.15,
                p95_latency=800.0,
                is_baseline=False,
            ))

        # Experiment 2: network_latency on ratings (mild impact)
        intent2 = ExperimentIntent(
            fault_type="network_latency",
            target_service="ratings",
            fault_params='{"latency_ms": 200}',
            rollback_command="toxiproxy-cli toxic remove",
            status="success",
            created_at=now - timedelta(minutes=3),
            expires_at=now + timedelta(minutes=5),
        )
        session.add(intent2)
        session.flush()

        session.add(ExperimentResult(
            intent_id=intent2.id,
            kill_switch_fired=False,
            recovery_time_sec=15.0,
            resilience_score=None,
        ))

        for i in range(5):
            session.add(MetricsSnapshot(
                timestamp=now - timedelta(minutes=2) + timedelta(seconds=i * 10),
                error_rate=0.02,
                p95_latency=180.0,
                is_baseline=False,
            ))

        session.commit()
        session.close()

    def test_report_generates_files(self):
        """Reporter produces report.json and REPORT.md."""
        data = generate_report(
            db_url=self.db_url,
            use_llm=False,
            output_dir=self.tmpdir,
        )

        json_path = Path(self.tmpdir) / "report.json"
        md_path = Path(self.tmpdir) / "REPORT.md"

        self.assertTrue(json_path.exists(), "report.json was not created")
        self.assertTrue(md_path.exists(), "REPORT.md was not created")

        # Verify JSON is valid
        with open(json_path) as f:
            loaded = json.load(f)
        self.assertEqual(loaded["total_experiments"], 2)

    def test_report_content_correctness(self):
        """Verify the report data has correct structure and values."""
        data = generate_report(
            db_url=self.db_url,
            use_llm=False,
            output_dir=self.tmpdir,
        )

        self.assertEqual(data["total_experiments"], 2)
        self.assertEqual(data["kill_switch_count"], 1)
        self.assertIn("cart", data["kill_switch_services"])

        # Weaknesses should be sorted worst-first
        self.assertEqual(len(data["weaknesses"]), 2)
        self.assertLessEqual(
            data["weaknesses"][0]["score"],
            data["weaknesses"][1]["score"],
        )

        # All scores should be in valid range
        for exp in data["experiments"]:
            self.assertGreaterEqual(exp["composite_score"], 0.0)
            self.assertLessEqual(exp["composite_score"], 1.0)
            self.assertGreaterEqual(exp["score_out_of_10"], 0.0)
            self.assertLessEqual(exp["score_out_of_10"], 10.0)

    def test_report_works_without_fastapi(self):
        """
        The entire pipeline should work with just the SQLite file.
        This test proves the report can be generated in the T-30 min window.
        """
        # We never started FastAPI — the reporter connects directly.
        data = generate_report(
            db_url=self.db_url,
            use_llm=False,
            output_dir=self.tmpdir,
        )
        self.assertIsNotNone(data)
        self.assertGreater(data["total_experiments"], 0)

    def test_heuristic_analysis_present(self):
        """Each experiment should have an analysis and recommended fix."""
        data = generate_report(
            db_url=self.db_url,
            use_llm=False,
            output_dir=self.tmpdir,
        )
        for exp in data["experiments"]:
            self.assertIsNotNone(exp["llm_analysis"])
            self.assertIsNotNone(exp["recommended_fix"])
            self.assertIn(exp["recommended_fix"], [
                "Increase Replicas",
                "Adjust Timeouts",
                "Add Retries",
                "Implement Circuit Breaker",
            ])

    def test_markdown_contains_key_sections(self):
        """REPORT.md should contain all expected sections."""
        generate_report(
            db_url=self.db_url,
            use_llm=False,
            output_dir=self.tmpdir,
        )
        md_path = Path(self.tmpdir) / "REPORT.md"
        content = md_path.read_text(encoding="utf-8")

        self.assertIn("Executive Summary", content)
        self.assertIn("Per-Experiment Results", content)
        self.assertIn("Ranked Weaknesses", content)
        self.assertIn("Platform Safety Summary", content)


if __name__ == "__main__":
    unittest.main()
