"""Load experiment runs (runs/*.json from scripts/day1_demo.py) into one normalised, PII-masked shape.

Handles both run formats:
  v1 (first day-1 runs)  windows carry only aggregate req/err
  v2 (day1_demo.py now)  windows also carry per-endpoint [requests, failures] ("ep"), plus baseline
                         per-endpoint totals, a free-text label ("after adding retries to cart") and window size
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from insights.pii import Masker

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNS_DIR = Path(os.environ.get("RUNS_DIR", REPO_ROOT / "runs"))
DEFAULT_WINDOW_S = 5.0
_TS = re.compile(r"(\d{8})_(\d{6})")


def _started_at(path: Path, raw: Dict[str, Any]) -> Optional[str]:
    if raw.get("started_at"):
        return raw["started_at"]
    m = _TS.search(path.stem)
    if m:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").isoformat()
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat()


def normalise(raw: Dict[str, Any], run_id: str, started_at: str) -> Dict[str, Any]:
    windows = []
    for w in raw.get("windows", []):
        req = int(w.get("req") or 0)
        err = float(w.get("err") or 0.0)
        windows.append({
            "t": float(w.get("t") or 0.0),
            "phase": w.get("phase", "fault"),
            "req": req,
            "fail": int(round(req * err)),
            "err": err,
            "p95": w.get("p95"),
            "failing": w.get("failing", []),
            "down": w.get("down", []),
            "ep": w.get("ep"),  # v2 only: {endpoint: [requests, failures]}
        })
    baseline = raw.get("baseline", {}) or {}
    params = raw.get("params") or {}
    return {
        "run_id": run_id,
        "started_at": started_at,
        "format": "v2" if any(w["ep"] for w in windows) else "v1",
        "label": raw.get("label") or "",
        "fault": raw.get("fault", "unknown"),
        "target": raw.get("target", "unknown"),
        "params": params,
        "signature": f"{raw.get('fault', 'unknown')}:{raw.get('target', 'unknown')}"
                     + (f":{json.dumps(params, sort_keys=True)}" if params else ""),
        "intent_id": raw.get("intent_id"),
        "window_s": float(raw.get("window_s") or DEFAULT_WINDOW_S),
        "baseline": {
            "requests": int(baseline.get("requests") or 0),
            "error_rate": float(baseline.get("error_rate") or 0.0),
            "p95_ms": baseline.get("p95_ms"),
            "threshold": float(baseline.get("threshold") or 0.05),
            "duration_s": float(baseline.get("duration_s") or 0.0) or None,
            "ep": baseline.get("ep"),  # v2 only
        },
        "windows": windows,
        "summary": raw.get("summary", {}) or {},
    }


def load_runs(runs_dir: Path = None, masker: Masker = None) -> List[Dict[str, Any]]:
    """All runs, oldest first, masked. Unreadable files are skipped (a half-written run must not break the UI)."""
    runs_dir = Path(runs_dir or DEFAULT_RUNS_DIR)
    masker = masker or Masker()
    out = []
    if not runs_dir.is_dir():
        return out
    for path in sorted(runs_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict) or "windows" not in raw:
            continue
        out.append(normalise(masker.mask_obj(raw), path.stem, _started_at(path, raw)))
    out.sort(key=lambda r: r["started_at"])
    return out
