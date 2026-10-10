# M2 - AI & Backend Lead Tasks

**Role Objective:** Build the command center that orchestrates chaos experiments, tracks state securely using SQLAlchemy/Alembic, maps the application dependency graph, and makes intelligent decisions (heuristic-first).

## Instructions for AGY CLI
Use these instructions to scaffold the Python backend.

### 1. Initialize FastAPI and SQLAlchemy + Alembic
- Create a Python FastAPI application in the `controller/` directory.
- **State Management:** Use SQLite. Set up `SQLAlchemy` as the ORM and `Alembic` for schema migrations from Day 1.
- Define the schemas precisely: `ExperimentIntent`, `MetricsSnapshot`, `ExperimentResult`, `Finding`.

### 2. Dependency Mapper (`make map`)
- Build a Python module that maps the target deployment (`robot-shop` or fallback `martian-bank`).
- **Static Analysis:** Parse the `sandbox/docker-compose.yml` (and override) for `depends_on` and network aliases.
- **Runtime Analysis:** Use `docker inspect` to gather container IPs and labels.
- Output a `graph.json` defining nodes (kind: gateway, frontend, worker, db, cache, queue) and edges.
- Provide an endpoint or CLI command (`make map`) that outputs this graph and renders it to a Mermaid diagram.

### 3. Candidate Generation & Heuristic Scoring (Primary Path)
Our primary requirement is to survive without LLM API access (`--no-llm`).
- Write a function to generate all valid combinations of `[Fault Type] x [Target Service]` based on the `graph.json`.
- Implement a heuristic scoring function based on node centrality, single point of failure status, and blast radius.
- Output a ranked list of candidates purely from the code logic (e.g., crashing `cart` is scored higher severity than `ratings`).

### 4. AI Re-ranking (Optional Day 2 Enhancement)
- Integrate the `google-genai` SDK using `gemini-2.5-flash`.
- Take the top 10 heuristically ranked candidates and send them to the LLM to be re-ranked based on learning value.
- Parse the structured JSON response: `[{candidate_id, rank, reason, hypothesis}]`.
- Ensure the code gracefully falls back to the heuristic rank if the API fails or the `--no-llm` flag is active.

### 5. Adaptive Loop & Magnitude Ramping
- Feed the result of the *last* experiment back into the context for the next decision.
- **Magnitude Ramping:** If injecting latency, start small (e.g., 200ms). Ramp up (800ms -> 2000ms) until a threshold is breached.
- If a hypothesis failed (the system didn't break), the planner should probe adjacent services.
