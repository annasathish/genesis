import json
from pathlib import Path

from mapper import generate_dependency_graph
from planner import generate_heuristic_candidates


def test_mapper_reads_configured_toxiproxy_links(tmp_path, monkeypatch):
    sandbox = tmp_path / "sandbox"
    proxy_dir = sandbox / "toxiproxy"
    proxy_dir.mkdir(parents=True)
    compose_file = sandbox / "docker-compose.yaml"
    compose_file.write_text(
        "services:\n  cart:\n    depends_on: [redis]\n  redis:\n",
        encoding="utf-8",
    )
    (proxy_dir / "toxiproxy.json").write_text(
        json.dumps([{
            "name": "cart_to_redis",
            "listen": "0.0.0.0:6379",
            "upstream": "redis:6379",
        }]),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    graph = generate_dependency_graph(str(compose_file))

    assert graph["proxies"] == [{
        "name": "cart_to_redis",
        "source": "cart",
        "target": "redis",
        "listen": "0.0.0.0:6379",
        "upstream": "redis:6379",
    }]


def test_planner_only_emits_supported_injector_targets():
    graph = {
        "nodes": [
            {"id": "mysql", "kind": "database"},
            {"id": "mongodb", "kind": "database"},
            {"id": "redis", "kind": "cache"},
            {"id": "rabbitmq", "kind": "queue"},
            {"id": "cart", "kind": "worker"},
        ],
        "proxies": [
            {"name": "cart_to_redis", "source": "cart", "target": "redis"},
        ],
    }

    candidates = generate_heuristic_candidates(graph)

    disk_targets = {
        candidate["target_service"]
        for candidate in candidates
        if candidate["fault_type"] == "disk_fill"
    }
    assert disk_targets == {"mysql", "mongodb"}
    assert any(
        candidate["fault_type"] == "rabbitmq_block"
        and candidate["target_service"] == "rabbitmq"
        for candidate in candidates
    )
    network_candidates = [c for c in candidates if c["fault_type"] == "network_latency"]
    assert [candidate["target_service"] for candidate in network_candidates] == ["cart_to_redis"]
    assert network_candidates[0]["display_target"] == "cart → redis (cart_to_redis)"
    assert all(candidate.get("hypothesis") for candidate in candidates)


def test_repo_topology_produces_only_configured_network_fault_targets(tmp_path, monkeypatch):
    repository = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(tmp_path)

    graph = generate_dependency_graph(str(repository / "sandbox" / "docker-compose.yaml"))
    candidates = generate_heuristic_candidates(graph)
    configured_names = {proxy["name"] for proxy in graph["proxies"]}
    planned_names = {
        candidate["target_service"]
        for candidate in candidates
        if candidate["fault_type"] == "network_latency"
    }

    assert planned_names == configured_names
