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
    You are an expert Chaos Engineer. Re-rank the following failure injection candidates based on their potential to reveal critical architectural weaknesses. 
    Return your response strictly as a JSON array of objects with keys: 'candidate_id', 'rank', 'reason', 'hypothesis'.
    Do not include markdown blocks or any other text.
    
    Candidates:
    {json.dumps(top_candidates, indent=2)}
    """
    
    try:
        model = genai.GenerativeModel(MODEL_NAME)
        response = model.generate_content(prompt)
        # Parse the JSON response
        # In a robust implementation, we would strip markdown ```json blocks here
        raw_text = response.text.strip().removeprefix("```json").removesuffix("```").strip()
        reranked = json.loads(raw_text)
        
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
