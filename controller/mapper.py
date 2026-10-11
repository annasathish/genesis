import yaml
import json
import os
from pathlib import Path

def generate_dependency_graph(compose_path: str):
    """
    Parses the docker-compose.yml file to build a static dependency graph.
    Returns nodes, Compose edges, and configured Toxiproxy links.
    """
    if not os.path.exists(compose_path):
        return json.dumps({"error": f"Compose file not found at {compose_path}"})

    with open(compose_path, 'r') as f:
        compose_data = yaml.safe_load(f)

    nodes = []
    edges = []
    
    services = compose_data.get('services', {})
    for service_name, config in services.items():
        config = config or {}
        # Heuristic to determine kind based on name/image
        kind = "worker"
        if "redis" in service_name or "cache" in service_name:
            kind = "cache"
        elif "db" in service_name or "mysql" in service_name or "mongo" in service_name:
            kind = "database"
        elif "web" in service_name or "frontend" in service_name:
            kind = "frontend"
        elif "rabbit" in service_name or "kafka" in service_name:
            kind = "queue"
            
        nodes.append({"id": service_name, "kind": kind})
        
        # Parse 'depends_on' for static edges
        depends_on = config.get('depends_on', [])
        if isinstance(depends_on, dict):
            # docker-compose v3+ dictionary format
            depends_on = list(depends_on.keys())
            
        for dependency in depends_on:
            edges.append({"source": service_name, "target": dependency})
            
    # Network faults need real Toxiproxy identifiers, which are link names
    # rather than Compose service names. Read the sandbox's checked-in list.
    proxy_path = Path(compose_path).resolve().parent / "toxiproxy" / "toxiproxy.json"
    proxies = []
    if proxy_path.is_file():
        with proxy_path.open("r", encoding="utf-8") as proxy_file:
            proxy_configs = json.load(proxy_file)
        for proxy in proxy_configs:
            name = proxy.get("name")
            if not name:
                continue
            source, separator, dependency = name.partition("_to_")
            proxies.append({
                "name": name,
                "source": source if separator else None,
                "target": dependency if separator else None,
                "listen": proxy.get("listen"),
                "upstream": proxy.get("upstream"),
            })

    graph = {"nodes": nodes, "edges": edges, "proxies": proxies}
    
    with open("graph.json", "w") as out:
        json.dump(graph, out, indent=2)
        
    return graph

if __name__ == "__main__":
    # Test execution
    # For hackathon, M1 will provide sandbox/docker-compose.yaml
    generate_dependency_graph("../sandbox/docker-compose.yaml")
