"""Run a dependency-ranked, bounded Day 2 experiment sequence on the local sandbox."""

import json
import os
import subprocess
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx


API_URL = os.environ.get("CONTROLLER_URL", "http://127.0.0.1:8000").rstrip("/") + "/api/v1"
EXPERIMENT_TTL_SECONDS = 30
RECOVERY_WAIT_SECONDS = EXPERIMENT_TTL_SECONDS + 10


def print_header(title: str) -> None:
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


def post(path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    response = httpx.post(f"{API_URL}{path}", json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def choose_diverse(candidates: Iterable[Dict[str, Any]], count: int) -> List[Dict[str, Any]]:
    """Keep high-ranked candidates while varying both fault and service."""
    chosen: List[Dict[str, Any]] = []
    used_targets = set()
    used_faults = set()
    for candidate in candidates:
        target = candidate.get("target_service")
        fault = candidate.get("fault_type")
        if not target or not fault or target in used_targets or fault in used_faults:
            continue
        chosen.append(candidate)
        used_targets.add(target)
        used_faults.add(fault)
        if len(chosen) == count:
            break
    return chosen


def main() -> None:
    print_header("1 · Map the sandbox and rank experiments")
    mapped = post("/map")
    print(mapped.get("message", "Dependency map updated."))
    plan = post("/plan").get("candidates", [])
    if not plan:
        raise RuntimeError("The planner returned no candidates.")

    ranked = choose_diverse(plan, 3)
    if len(ranked) < 2:
        raise RuntimeError("The planner did not return two distinct service/fault candidates.")

    for index, candidate in enumerate(ranked, start=1):
        print(
            f"{index}. {candidate['fault_type']} → "
            f"{candidate.get('display_target', candidate['target_service'])} | "
            f"{candidate.get('reasoning', candidate.get('reason', 'Ranked from the dependency map.'))}"
        )
        if candidate.get("hypothesis"):
            print(f"   Hypothesis: {candidate['hypothesis']}")

    safety = httpx.get(f"{API_URL}/safety/status", timeout=10)
    safety.raise_for_status()
    state = safety.json()
    if not state.get("is_safe_to_inject"):
        raise RuntimeError(f"Safety preflight rejected experiments: {state.get('safety_reason')}")

    print_header("2 · Run bounded experiments one at a time")
    for index, candidate in enumerate(ranked, start=1):
        print(f"Experiment {index}: {candidate['fault_type']} on {candidate['target_service']}")
        result = post(
            "/inject",
            {
                "fault_type": candidate["fault_type"],
                "target": candidate["target_service"],
                "ttl_seconds": EXPERIMENT_TTL_SECONDS,
            },
        )
        print(json.dumps(result["result"], indent=2))
        print(f"Waiting {RECOVERY_WAIT_SECONDS}s for TTL rollback and recovery telemetry…")
        time.sleep(RECOVERY_WAIT_SECONDS)

    print_header("3 · Run one reviewed combined experiment")
    pair: Optional[Tuple[Dict[str, Any], Dict[str, Any]]] = None
    for first_index, first in enumerate(plan):
        for second in plan[first_index + 1 :]:
            if (
                first.get("target_service") != second.get("target_service")
                and first.get("fault_type") != second.get("fault_type")
            ):
                pair = first, second
                break
        if pair:
            break
    if pair:
        faults = [
            {
                "fault_type": candidate["fault_type"],
                "target": candidate["target_service"],
                "ttl_seconds": EXPERIMENT_TTL_SECONDS,
            }
            for candidate in pair
        ]
        print("Combined test:", " + ".join(f"{f['fault_type']} on {f['target']}" for f in faults))
        result = post(
            "/inject/combined",
            {"faults": faults, "ttl_seconds": EXPERIMENT_TTL_SECONDS},
        )
        print(json.dumps(result, indent=2))
        if result.get("status") != "success":
            raise RuntimeError("Combined experiment partially failed; inspect the result before continuing.")
        time.sleep(RECOVERY_WAIT_SECONDS)
    else:
        print("No distinct service/fault pair was available; skipping combined injection.")

    print_header("4 · Generate the evidence report")
    subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            ".env",
            "-f",
            "controller/docker-compose.yml",
            "exec",
            "api",
            "python",
            "cli.py",
            "report",
            "--llm",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
