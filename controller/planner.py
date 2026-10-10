import json
import os
import google.generativeai as genai
from typing import List, Dict

# Set up Gemini
genai.configure(api_key=os.environ.get("GEMINI_API_KEY", "DUMMY_KEY"))
# Using the correct model per the user's architectural review
MODEL_NAME = "gemini-2.5-flash"

AVAILABLE_FAULTS = ["container_crash", "network_latency", "disk_fill"]

def generate_heuristic_candidates(graph: dict) -> List[Dict]:
    """
    Primary Path (--no-llm): Generates and scores candidates purely via code.
    """
    nodes = graph.get("nodes", [])
    candidates = []
    candidate_id = 1
    
    for node in nodes:
        service = node["id"]
        kind = node.get("kind", "worker")
        
        # Calculate heuristic risk score based on service kind
        # Databases and Queues are High Risk (SPOF)
        if kind in ["database", "cache", "queue"]:
            base_score = 80
        elif kind == "frontend":
            base_score = 20
        else:
            base_score = 50
            
        for fault in AVAILABLE_FAULTS:
            # Disk fill only makes sense for stateful services
            if fault == "disk_fill" and kind not in ["database", "cache", "queue"]:
                continue
                
            candidates.append({
                "candidate_id": candidate_id,
                "target_service": service,
                "fault_type": fault,
                "heuristic_score": base_score,
                "reasoning": f"Heuristic: {kind} node subjected to {fault}."
            })
            candidate_id += 1
            
    # Sort by risk score descending
    candidates.sort(key=lambda x: x["heuristic_score"], reverse=True)
    return candidates

def llm_rerank(candidates: List[Dict]) -> List[Dict]:
    """
    Optional Enhancement: Passes the top heuristic candidates to Gemini for re-ranking.
    """
    if os.environ.get("USE_LLM", "false").lower() == "false":
        return candidates
        
    top_candidates = candidates[:10]
    
    prompt = f"""
    You are an expert Chaos Engineer. Re-rank the following failure injection candidates based on their potential to reveal critical architectural weaknesses (Single Points of Failure). 

    Return your response strictly as a JSON array of objects with keys: 'candidate_id', 'rank', 'reason', 'hypothesis'.
    
    EXAMPLE GOOD HYPOTHESIS:
    "If the RabbitMQ queue goes down, the payment gateway will timeout, causing the upstream web UI to freeze rather than gracefully degrading."
    
    Candidates to re-rank:
    {json.dumps(top_candidates, indent=2)}
    """
    
    try:
        model = genai.GenerativeModel(MODEL_NAME)
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                response_mime_type="application/json"
            )
        )
        # Parse the JSON response
        reranked = json.loads(response.text)

        
        # Merge LLM ranking back with candidate details
        final_list = []
        for rank_data in sorted(reranked, key=lambda x: x["rank"]):
            # Find original candidate
            orig = next((c for c in top_candidates if c["candidate_id"] == rank_data["candidate_id"]), None)
            if orig:
                orig["llm_rank"] = rank_data["rank"]
                orig["hypothesis"] = rank_data["hypothesis"]
                orig["reasoning"] = rank_data["reason"]
                final_list.append(orig)
                
        return final_list
    except Exception as e:
        print(f"LLM re-ranking failed, falling back to heuristics. Error: {e}")
        return candidates

def generate_adaptive_candidate(last_intent: dict, last_result: dict, graph: dict) -> dict:
    """
    Implements the Adaptive Loop (Magnitude Ramping).
    Takes the result of the last experiment. If the hypothesis failed (e.g., system didn't break),
    it ramps up the magnitude or probes adjacent services.
    """
    fault_type = last_intent.get("fault_type")
    target = last_intent.get("target_service")
    params = json.loads(last_intent.get("fault_params", "{}"))
    kill_switch_fired = last_result.get("kill_switch_fired", False)
    
    # Scenario 1: We tried a network latency attack, and the system survived perfectly.
    # Adaptive Action: Ramp up the magnitude (e.g., 200ms -> 800ms)
    if not kill_switch_fired and fault_type == "network_latency":
        current_latency = params.get("latency_ms", 200)
        new_latency = current_latency + 50  # Gentle ramp up
        
        # Clamp to a maximum reasonable value to prevent infinite ramping
        new_latency = min(new_latency, 3000)
        
        return {
            "candidate_id": last_intent.get("id", 0) + 1000,
            "target_service": target,
            "fault_type": fault_type,
            "fault_params": json.dumps({"latency_ms": new_latency}),
            "reasoning": f"Adaptive Loop: Target {target} survived {current_latency}ms latency. Ramping magnitude to {new_latency}ms to find the breaking point."
        }
        
    # Scenario 2: The system broke (Kill switch fired).
    # Adaptive Action: The hypothesis was correct. Now probe an adjacent service in the graph.
    if kill_switch_fired:
        edges = graph.get("edges", [])
        # Find a service that depends on the target or that the target depends on
        adjacent_services = [e["target"] for e in edges if e["source"] == target]
        if not adjacent_services:
            adjacent_services = [e["source"] for e in edges if e["target"] == target]
            
        next_target = adjacent_services[0] if adjacent_services else "frontend"
        
        return {
            "candidate_id": last_intent.get("id", 0) + 1000,
            "target_service": next_target,
            "fault_type": "container_crash", # Pivot to a different fault type
            "fault_params": "{}",
            "reasoning": f"Adaptive Loop: Target {target} successfully failed. Shifting blast radius investigation to adjacent node: {next_target}."
        }
        
    return None
