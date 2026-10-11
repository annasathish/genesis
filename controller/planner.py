"""Dependency-aware, bounded experiment planning."""

import json
import os
from typing import Dict, List, Optional

from ai_client import generate_json

AVAILABLE_FAULTS = ["container_crash", "network_latency", "disk_fill", "rabbitmq_block"]
DISK_FILL_TARGETS = {"mysql", "mongodb"}
RABBITMQ_TARGETS = {"rabbitmq"}


def generate_heuristic_candidates(graph: dict) -> List[Dict]:
    """Generate only experiments whose injector targets exist in this sandbox."""
    nodes = graph.get("nodes", [])
    node_by_id = {node.get("id"): node for node in nodes if node.get("id")}
    candidates: List[Dict] = []
    candidate_id = 1

    for node in nodes:
        service = node.get("id")
        if not service:
            continue
        kind = node.get("kind", "worker")
        if kind in {"database", "cache", "queue"}:
            base_score = 80
        elif kind == "frontend":
            base_score = 20
        else:
            base_score = 50

        candidates.append({
            "candidate_id": candidate_id,
            "target_service": service,
            "display_target": service,
            "fault_type": "container_crash",
            "target_kind": "service",
            "heuristic_score": base_score,
            "reasoning": f"Test how callers handle the loss of {service}.",
            "hypothesis": f"If {service} becomes unavailable, its callers should fail safely and recover when it returns.",
        })
        candidate_id += 1

        # Match the actual injector's allowlist instead of inferring support
        # from a broad service category such as database or cache.
        if service in DISK_FILL_TARGETS:
            candidates.append({
                "candidate_id": candidate_id,
                "target_service": service,
                "display_target": service,
                "fault_type": "disk_fill",
                "target_kind": "service",
                "heuristic_score": base_score + 5,
                "reasoning": f"Test whether {service} handles bounded pressure on its approved temporary filesystem.",
                "hypothesis": f"If {service}'s temporary storage fills, it should reject or shed work cleanly and recover after cleanup.",
            })
            candidate_id += 1

        if service in RABBITMQ_TARGETS:
            candidates.append({
                "candidate_id": candidate_id,
                "target_service": service,
                "display_target": service,
                "fault_type": "rabbitmq_block",
                "target_kind": "service",
                "heuristic_score": base_score + 10,
                "reasoning": "Test whether asynchronous order processing handles a broker outage and resumes safely.",
                "hypothesis": "If RabbitMQ stops processing, publishers and consumers should recover without losing or duplicating orders.",
            })
            candidate_id += 1

    # NetworkLatencyFault requires a Toxiproxy proxy identifier. The mapper
    # supplies these from the sandbox's checked-in Toxiproxy configuration.
    for proxy in graph.get("proxies", []):
        name = proxy.get("name")
        if not name:
            continue
        source = proxy.get("source") or "caller"
        dependency = proxy.get("target") or "dependency"
        dependency_kind = node_by_id.get(dependency, {}).get("kind", "worker")
        score = 70 if dependency_kind in {"database", "cache", "queue"} else 55
        candidates.append({
            "candidate_id": candidate_id,
            "target_service": name,
            "display_target": f"{source} → {dependency} ({name})",
            "fault_type": "network_latency",
            "target_kind": "network_link",
            "source_service": source,
            "dependency_service": dependency,
            "heuristic_score": score,
            "reasoning": f"Test the {source} → {dependency} call path with bounded network delay.",
            "hypothesis": f"If traffic from {source} to {dependency} slows, callers should time out or degrade before latency spreads upstream.",
        })
        candidate_id += 1

    candidates.sort(key=lambda candidate: candidate["heuristic_score"], reverse=True)
    return candidates


def llm_rerank(candidates: List[Dict]) -> List[Dict]:
    """Optionally rerank valid candidates; deterministic ordering is the fallback."""
    if (
        os.environ.get("USE_LLM", "false").lower() == "false"
        or not os.environ.get("GEMINI_API_KEY")
    ):
        return candidates

    top_candidates = candidates[:10]
    prompt = f"""
    You are an expert chaos engineer. Rerank the supplied, already-validated
    candidates by their likely value for finding a resilience weakness.
    Preserve the candidate IDs. Return a JSON array with candidate_id, rank,
    reason, and hypothesis for each candidate. Do not invent targets or faults.

    Candidates:
    {json.dumps(top_candidates, indent=2)}
    """

    try:
        reranked = json.loads(generate_json(prompt))
        if not isinstance(reranked, list):
            raise ValueError("Gemini response was not a JSON array")

        final_list: List[Dict] = []
        used_ids = set()
        valid_rows = [row for row in reranked if isinstance(row, dict)]
        for rank_data in sorted(valid_rows, key=lambda row: row.get("rank", 10**9)):
            candidate_id = rank_data.get("candidate_id")
            original = next(
                (candidate for candidate in top_candidates if candidate["candidate_id"] == candidate_id),
                None,
            )
            if not original or candidate_id in used_ids:
                continue
            used_ids.add(candidate_id)
            ranked = dict(original)
            ranked["llm_rank"] = rank_data.get("rank")
            if isinstance(rank_data.get("hypothesis"), str):
                ranked["hypothesis"] = rank_data["hypothesis"]
            if isinstance(rank_data.get("reason"), str):
                ranked["reasoning"] = rank_data["reason"]
            final_list.append(ranked)

        final_list.extend(candidate for candidate in candidates if candidate["candidate_id"] not in used_ids)
        return final_list
    except Exception as exc:
        print(f"LLM re-ranking failed, falling back to heuristics: {exc}")
        return candidates


def generate_adaptive_candidate(last_intent: dict, last_result: dict, graph: dict) -> Optional[dict]:
    """Gently ramp a surviving network fault or pivot to an adjacent service."""
    fault_type = last_intent.get("fault_type")
    target = last_intent.get("target_service")
    try:
        params = json.loads(last_intent.get("fault_params") or "{}")
    except (TypeError, json.JSONDecodeError):
        params = {}
    kill_switch_fired = last_result.get("kill_switch_fired", False)

    if not kill_switch_fired and fault_type == "network_latency":
        current_latency = int(params.get("latency_ms", 200))
        new_latency = min(current_latency + 50, 3000)
        proxy = next((item for item in graph.get("proxies", []) if item.get("name") == target), {})
        source, dependency = proxy.get("source"), proxy.get("target")
        display_target = f"{source} → {dependency} ({target})" if source and dependency else target
        return {
            "candidate_id": last_intent.get("id", 0) + 1000,
            "target_service": target,
            "display_target": display_target,
            "fault_type": fault_type,
            "fault_params": json.dumps({"latency_ms": new_latency}),
            "reasoning": f"The system survived {current_latency}ms on this link; increase gently to {new_latency}ms.",
        }

    if kill_switch_fired:
        proxy = next((item for item in graph.get("proxies", []) if item.get("name") == target), None)
        pivot_service = proxy.get("source") if proxy else target
        edges = graph.get("edges", [])
        adjacent = [edge.get("target") for edge in edges if edge.get("source") == pivot_service]
        adjacent += [edge.get("source") for edge in edges if edge.get("target") == pivot_service]
        node_ids = {node.get("id") for node in graph.get("nodes", []) if node.get("id")}
        next_target = next((service for service in adjacent if service in node_ids), None)
        if not next_target:
            next_target = next((node["id"] for node in graph.get("nodes", []) if node.get("kind") == "frontend"), None)
        if not next_target:
            next_target = next(iter(node_ids), "web")
        return {
            "candidate_id": last_intent.get("id", 0) + 1000,
            "target_service": next_target,
            "display_target": next_target,
            "fault_type": "container_crash",
            "fault_params": "{}",
            "reasoning": f"The kill switch contained {target}; probe adjacent service {next_target} next.",
        }

    return None
