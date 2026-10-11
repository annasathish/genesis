"""
Baseline Capture Module for Genesis Chaos Controller (M4 - Safety & Observability)

Defines the steady-state baseline before chaos experiments occur:
- Configurable duration (default 600s) and polling interval (default 5s).
- Computes mean (μ) and standard deviation (σ).
- Computes tolerance bands: mean ± max(3σ, 20% of mean).
- Persists baseline.json for the Kill Switch and stores points in SQLite.
- Fully non-fabricating: fails safely if Locust is offline or data cannot be collected.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import math
import os
import signal
import sys
from typing import Any, Dict, List, Optional

# Ensure controller directory is in sys.path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from telemetry import LocustClient, DockerMonitor, TelemetryCollector, TelemetrySnapshot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("controller.baseline")


def compute_bands(values: List[float]) -> Dict[str, float]:
    """
    Computes baseline statistical bands:
    band_margin = max(3 * σ, 0.20 * μ)
    acceptable band = [μ - band_margin, μ + band_margin]
    """
    if not values:
        raise ValueError("Cannot compute baseline bands from an empty dataset")

    n = len(values)
    mean_val = sum(values) / n

    if n > 1:
        variance = sum((x - mean_val) ** 2 for x in values) / (n - 1)
        std_val = math.sqrt(variance)
    else:
        std_val = 0.0

    band_margin = max(3.0 * std_val, 0.20 * mean_val)
    upper_bound = mean_val + band_margin
    lower_bound = max(0.0, mean_val - band_margin)

    return {
        "sample_count": n,
        "mean": round(mean_val, 6),
        "std": round(std_val, 6),
        "band_margin": round(band_margin, 6),
        "upper_bound": round(upper_bound, 6),
        "lower_bound": round(lower_bound, 6),
    }


class BaselineCollector:
    """
    Orchestrates steady-state telemetry collection and band computation.
    """

    def __init__(
        self,
        duration_seconds: float = 600.0,
        interval_seconds: float = 5.0,
        locust_url: str = "http://localhost:8089",
        output_file: str = "baseline.json",
        db_path: Optional[str] = None,
        telemetry_collector: Optional[TelemetryCollector] = None,
        db_url: Optional[str] = None,
    ):
        self.duration_seconds = float(duration_seconds)
        self.interval_seconds = float(interval_seconds)
        self.output_file = output_file
        self.db_path = db_path or os.path.join(CURRENT_DIR, "chaos_state.db")
        self.db_url = db_url or f"sqlite:///{self.db_path}"

        if telemetry_collector is not None:
            self.collector = telemetry_collector
        else:
            self.collector = TelemetryCollector(
                locust_client=LocustClient(base_url=locust_url),
                docker_monitor=DockerMonitor(),
            )

        self._shutdown_event = asyncio.Event()

    def request_shutdown(self) -> None:
        """Triggers graceful early shutdown upon signal."""
        logger.warning("Graceful shutdown requested for baseline capture.")
        self._shutdown_event.set()

    def _get_db_session(self):
        """Attempts to open a SQLite database session for snapshot storage."""
        try:
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker
            from models import Base

            engine = create_engine(self.db_url, connect_args={"check_same_thread": False})
            Base.metadata.create_all(bind=engine)
            session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
            return session_factory()
        except Exception as exc:
            logger.info("Database session unavailable (skipping DB persistence): %s", exc)
            return None

    async def run(self) -> Dict[str, Any]:
        """
        Executes the collection loop.
        Returns the computed baseline bands and writes output_file.
        """
        logger.info(
            "Starting baseline capture: duration=%.1fs, interval=%.1fs, output=%s",
            self.duration_seconds,
            self.interval_seconds,
            self.output_file,
        )

        snapshots: List[TelemetrySnapshot] = []
        p95_samples: List[float] = []
        error_rate_samples: List[float] = []

        start_time = asyncio.get_running_loop().time()
        deadline = start_time + self.duration_seconds
        sample_index = 0
        db_session = self._get_db_session()

        try:
            while not self._shutdown_event.is_set():
                now = asyncio.get_running_loop().time()
                if now >= deadline:
                    break

                sample_index += 1
                snapshot = await self.collector.collect_snapshot()
                snapshots.append(snapshot)

                if snapshot.locust.available:
                    if snapshot.p95_latency is not None:
                        p95_samples.append(snapshot.p95_latency)
                    if snapshot.error_rate is not None:
                        error_rate_samples.append(snapshot.error_rate)

                    logger.info(
                        "Sample #%d: p95=%.1fms, err=%.2f%%, rps=%.1f (Locust OK)",
                        sample_index,
                        snapshot.p95_latency or 0.0,
                        (snapshot.error_rate or 0.0) * 100,
                        snapshot.locust.current_rps,
                    )
                else:
                    logger.warning(
                        "Sample #%d: Locust unavailable (%s)",
                        sample_index,
                        snapshot.locust.error_message,
                    )

                if db_session:
                    self.collector.persist_snapshot(snapshot, db_session, is_baseline=True)

                # Sleep until next interval or shutdown
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=self.interval_seconds,
                    )
                    break
                except asyncio.TimeoutError:
                    pass

        finally:
            if db_session:
                db_session.close()

        # Validate that real telemetry was captured
        if not p95_samples or not error_rate_samples:
            error_msg = (
                f"Failed to capture baseline: Locust was unreachable throughout collection "
                f"({len(snapshots)} samples attempted, 0 valid telemetry points). "
                f"Check that the target app load generator is active."
            )
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        # Compute bands
        p95_bands = compute_bands(p95_samples)
        error_bands = compute_bands(error_rate_samples)

        # Kill Switch Thresholds per M4 specification:
        # 1. Error rate breach: error_rate > 5% (or upper bound if baseline is higher)
        # 2. Latency breach: current_p95 > 5 * baseline_p95
        kill_switch_error_threshold = max(0.05, error_bands["upper_bound"])
        kill_switch_p95_threshold = round(5.0 * p95_bands["mean"], 4)

        result: Dict[str, Any] = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": self.duration_seconds,
            "interval_seconds": self.interval_seconds,
            "sample_count": len(p95_samples),
            "p95_latency_ms": p95_bands,
            "error_rate": error_bands,
            "kill_switch_thresholds": {
                "max_error_rate": kill_switch_error_threshold,
                "p95_multiplier": 5.0,
                "p95_latency_threshold_ms": kill_switch_p95_threshold,
                "blind_guardrail_timeout_sec": 15,
                "consecutive_breach_windows": 2,
            },
        }

        # Write to JSON file
        output_path = self.output_file
        if not os.path.isabs(output_path):
            output_path = os.path.join(CURRENT_DIR, output_path)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)

        logger.info("Baseline saved successfully to %s", output_path)
        return result


def parse_args():
    parser = argparse.ArgumentParser(
        description="Capture steady-state baseline for AI Chaos Controller (M4)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=float(os.environ.get("BASELINE_DURATION_SEC", 600.0)),
        help="Duration in seconds (default: 600s / 10 minutes)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("BASELINE_INTERVAL_SEC", 5.0)),
        help="Polling interval in seconds (default: 5s)",
    )
    parser.add_argument(
        "--locust-url",
        type=str,
        default=os.environ.get("LOCUST_URL", "http://localhost:8089"),
        help="Locust base URL (default: http://localhost:8089)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="baseline.json",
        help="Output baseline JSON path (default: baseline.json)",
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="Quick verification mode: runs 10 seconds with 2-second interval",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    duration = 10.0 if args.test_mode else args.duration
    interval = 2.0 if args.test_mode else args.interval

    collector = BaselineCollector(
        duration_seconds=duration,
        interval_seconds=interval,
        locust_url=args.locust_url,
        output_file=args.output,
        db_url=os.environ.get("CHAOS_DB_URL") or os.environ.get("DB_PATH"),
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Attach signal handlers for graceful shutdown on POSIX systems
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, collector.request_shutdown)

    try:
        loop.run_until_complete(collector.run())
    except KeyboardInterrupt:
        logger.warning("Baseline collection interrupted by user.")
    except Exception as exc:
        logger.error("Baseline collection failed: %s", exc)
        sys.exit(1)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
