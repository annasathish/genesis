# Architecture Overview: Resource-Aware Autonomous Chaos

## 1. The Core Philosophy
Our system was engineered specifically for constrained environments (originally capped at 5.2GB RAM). Because standard observability stacks (Prometheus, Grafana, OpenTelemetry) consume massive amounts of memory, we engineered a highly efficient, bespoke telemetry and AI loop using Python, SQLite, and Locust.

## 2. System Components (The 5 Agents)

1. **The Arena (M1 - Target Environment):** 
   - **Stan's Robot Shop:** A 10-tier microservice architecture.
   - **Toxiproxy:** Injected as a sidecar to simulate degraded network connections without requiring host-level `iptables` privileges.

2. **The Brain (M2 - AI Controller):**
   - **Static Mapper:** Parses `docker-compose.yaml` to build a deterministic graph of service dependencies. This prevents LLM hallucinations.
   - **Two-Tier Planner:** Uses offline Python heuristics to score risk (e.g., databases are inherently riskier than frontends). It passes the top targets to **Gemini Flash** (current model via `gemini-flash-latest`, automatic fallback) to select the final attack strategy and blast radius.

3. **The Trigger (M3 - Execution Engine):**
   - An asynchronous Python manager that uses the Docker SDK and Toxiproxy API to execute faults (Latency, Crash, Disk Fill via `tmpfs`, Queue Blocking).

4. **The Guard (M4 - Telemetry & Safety):**
   - **Locust Monitor:** Generates synthetic user traffic and streams P95 Latency and Error Rates to the SQLite database.
   - **The Kill Switch:** A background watchdog. If host memory spikes dangerously or error rates reach 100%, it bypasses the AI and issues a hardware-level rollback command to Docker.

5. **The Analyst (M5 - Reporting & UI):**
   - **Live Dashboard:** `controller/insights/dashboard` (`make dashboard`, http://127.0.0.1:8050; the controller's `/dashboard` redirects there): live system map, human-approved experiment console with emergency stop, vulnerability board, run-to-run improvement with significance tests, business impact, Copilot chat.
   - **Multi-agent findings report:** a Planner, an Analyzer and a Recommender agent (`controller/agents/`). Python computes every number; the agents explain, rank and recommend, and AI text is accepted only if every number in it appears in the measured data.
   - **Business impact:** measured checkout loss projected onto production traffic with stated assumptions (`controller/insights/business_config.json`).

## 3. How the AI Makes Decisions
The AI operates on a continuous feedback loop:
1. **Map:** Generate the dependency graph.
2. **Score:** Assign risk weights based on service type.
3. **Plan:** Gemini outputs a JSON payload containing the exact attack vectors.
4. **Act & Observe:** Execute the fault, measure the latency spike, and rollback when safety thresholds are breached.
5. **Analyze:** the Analyzer and Recommender agents review the before/after metrics to rank weaknesses and generate architectural fix recommendations, each with the command that verifies it.
6. **Verify:** after a fix, the same experiment is re-run; runs are pooled per phase and compared with a two-proportion z-test, so an improvement is only claimed when it is significant.
