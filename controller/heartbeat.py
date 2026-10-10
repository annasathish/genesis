"""
Heartbeat Manager for Genesis Chaos Controller (M4 - Safety & Observability)

Writes periodic heartbeats to a shared file (and/or SQLite) every 5 seconds.
Enables the Dead-Man Watchdog to detect controller hangs or crashes (>20s).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("controller.heartbeat")

DEFAULT_HEARTBEAT_FILE = os.environ.get("HEARTBEAT_FILE", "heartbeat.json")


class HeartbeatManager:
    """
    Manages atomic writing and reading of controller heartbeats.
    """

    def __init__(self, filepath: Optional[str] = None):
        self.filepath = os.path.abspath(filepath or DEFAULT_HEARTBEAT_FILE)
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def write_heartbeat(self, status: str = "ok", extra: Optional[Dict[str, Any]] = None) -> float:
        """
        Atomically writes heartbeat timestamp and metadata to shared storage.
        Returns the epoch timestamp written.
        """
        now_epoch = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()

        payload = {
            "timestamp": now_iso,
            "epoch": now_epoch,
            "status": status,
            "pid": os.getpid(),
            "metadata": extra or {},
        }

        target_dir = os.path.dirname(self.filepath) or "."
        os.makedirs(target_dir, exist_ok=True)

        # Atomic file write pattern: write to temporary file in same directory, then replace
        temp_fd, temp_path = tempfile.mkstemp(dir=target_dir, prefix="hb_", suffix=".tmp")
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(temp_path, self.filepath)
        except Exception as exc:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            logger.error("Failed to write heartbeat file at %s: %s", self.filepath, exc)
            raise

        return now_epoch

    def read_heartbeat(self) -> Tuple[bool, float, Dict[str, Any]]:
        """
        Reads heartbeat and calculates age in seconds.
        Returns (exists: bool, age_seconds: float, payload: dict).
        """
        if not os.path.exists(self.filepath):
            return False, float("inf"), {}

        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            epoch = data.get("epoch")
            if epoch is None:
                # Fallback to parse ISO timestamp
                iso_ts = data.get("timestamp")
                if iso_ts:
                    parsed = datetime.fromisoformat(iso_ts)
                    epoch = parsed.timestamp()
                else:
                    return True, float("inf"), data

            age = max(0.0, time.time() - float(epoch))
            return True, age, data
        except Exception as exc:
            logger.warning("Error reading heartbeat from %s: %s", self.filepath, exc)
            return False, float("inf"), {"error": str(exc)}

    async def run_loop(self, interval_seconds: float = 5.0) -> None:
        """
        Runs async periodic loop writing heartbeats every interval_seconds.
        """
        self._running = True
        logger.info("Heartbeat loop started (writing every %.1fs to %s)", interval_seconds, self.filepath)
        try:
            while self._running:
                self.write_heartbeat()
                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("Heartbeat loop received cancellation request.")
        finally:
            self._running = False

    def start_background(self, interval_seconds: float = 5.0) -> asyncio.Task:
        """Starts the heartbeat loop as an asyncio background task."""
        if self._task and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self.run_loop(interval_seconds))
        return self._task

    def stop(self) -> None:
        """Stops the heartbeat loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
