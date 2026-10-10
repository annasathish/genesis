# Architecture Overview: Resource-Aware Autonomous Chaos

## 1. The Core Philosophy
Our system was engineered specifically for constrained environments (originally capped at 5.2GB RAM). Because standard observability stacks (Prometheus, Grafana, OpenTelemetry) consume massive amounts of memory, we engineered a highly efficient, bespoke telemetry and AI loop using Python, SQLite, and Locust.

## 2. System Components (The 5 Agents)

1. **The Arena (M1 - Target Environment):** 
   - **Stan's Robot Shop:** A 10-tier microservice architecture.
   - **Toxiproxy:** Injected as a sidecar to simulate degraded network connections without requiring host-level `iptables` privileges.

2. **The Brain (M2 - AI Controller):**
   - **Static Mapper:** Parses `docker-compose.yaml` to build a deterministic graph of service dependencies. This prevents LLM hallucinations.
   - **Two-Tier Planner:** Uses offline Python heuristics to score risk (e.g., databases are inherently riskier than frontends). It passes the top targets to **Gemini 2.5 Flash** to select the final attack strategy and blast radius.

3. **The Trigger (M3 - Execution Engine):**
   - An asynchronous Python manager that uses the Docker SDK and Toxiproxy API to execute faults (Latency, Crash, Disk Fill via `tmpfs`, Queue Blocking).

4. **The Guard (M4 - Telemetry & Safety):**
   - **Locust Monitor:** Generates synthetic user traffic and streams P95 Latency and Error Rates to the SQLite database.
   - **The Kill Switch:** A background watchdog. If host memory spikes dangerously or error rates reach 100%, it bypasses the AI and issues a hardware-level rollback command to Docker.

5. **The Analyst (M5 - Reporting & UI):**
   - **Live Dashboard:** A lightweight HTML/Tailwind frontend that polls the API for live metrics.
   - **ROI Translation Engine:** Uses Gemini to translate container crashes into business impact (e.g., translating a Redis crash into estimated Revenue at Risk and User Churn).

## 3. How the AI Makes Decisions
The AI operates on a continuous feedback loop:
1. **Map:** Generate the dependency graph.
2. **Score:** Assign risk weights based on service type.
3. **Plan:** Gemini outputs a JSON payload containing the exact attack vectors.
4. **Act & Observe:** Execute the fault, measure the latency spike, and rollback when safety thresholds are breached.
5. **Analyze:** Gemini reviews the before/after metrics to generate architectural fix recommendations.
