import yaml
import json
import os

def generate_dependency_graph(compose_path: str):
    """
    Parses the docker-compose.yml file to build a static dependency graph.
    Returns a JSON string representing nodes and edges.
    """
    if not os.path.exists(compose_path):
        return json.dumps({"error": f"Compose file not found at {compose_path}"})

    with open(compose_path, 'r') as f:
        compose_data = yaml.safe_load(f)

    nodes = []
    edges = []
    
    services = compose_data.get('services', {})
    for service_name, config in services.items():
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
            
    graph = {"nodes": nodes, "edges": edges}
    
    with open("graph.json", "w") as out:
        json.dump(graph, out, indent=2)
        
    return graph

if __name__ == "__main__":
    # Test execution
    # For hackathon, M1 will provide sandbox/docker-compose.yaml
    generate_dependency_graph("../sandbox/docker-compose.yaml")
