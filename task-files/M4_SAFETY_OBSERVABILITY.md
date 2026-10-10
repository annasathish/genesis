# 🌍 Global Project Context for AGY CLI

**Project:** AI-Driven Chaos Engineering Platform
**Hackathon Goal:** Build an autonomous system that intelligently injects controlled failures into a local sandbox environment, observes impact, and outputs a resilience score.
**Target App:** Stan's Robot Shop (`instana/robot-shop`) [Fallback: Cisco's Martian Bank]
**Strict Hardware Constraint:** The host Windows WSL2 machine has only **5.2 GB of free RAM**. You MUST prioritize lightweight code, async non-blocking operations, and strict memory caps.
**System Architecture:**
- `sandbox/`: The isolated docker-compose project running the target application.
- `controller/`: The isolated docker-compose project running the FastAPI/SQLite chaos backend.
**Team Structure:** This project is split across 5 roles (M1-M5). You are executing one specific piece. Ensure your code exposes clean interfaces (like SQLite schemas or Makefile targets) so the other 4 AGY agents can easily integrate with your work.

---

# M4 - Safety & Observability Tasks

**Role Objective:** Monitor the live state of the application, capture the steady-state baseline, and enforce hard safety limits via the Kill Switch and Watchdog.

## Instructions for AGY CLI
Use these instructions to scaffold the telemetry and safety systems.

### 1. Telemetry Ingestion
- Rely exclusively on Locust for user-facing metrics and the Docker SDK for container health.
- `robot-shop` comes with a `load` container. Either parse its output, or spin up a dedicated headless Locust instance pointing at the `web` container.
- Write an async loop that continuously polls the Locust stats. Extract total error rate and p95 latency.

### 2. Baseline Capture (`make baseline`)
You are responsible for defining the steady-state baseline before any chaos occurs.
- Create a script/endpoint triggered by `make baseline`.
- The script should run for 10 minutes (600s), polling Locust and Docker stats every 5 seconds.
- **Compute Bands:** Calculate the `mean` and standard deviation (`σ`). Set the acceptable band to `mean ± max(3σ, 20% of mean)` for latency and error rate.
- **Output:** Save `baseline.json` containing these bands to be used by the Kill Switch.

### 3. The Kill Switch (State Machine)
- The Kill Switch runs as a non-blocking async background task in FastAPI.
- **Trigger:** The switch trips if telemetry breaches the computed baseline bands (e.g., `error_rate > 5%` or `current_p95 > 5 * baseline_p95`).
- **Consecutive Windows:** Require a breach for **2 consecutive 5-second windows** to avoid false positives on random spikes.
- **Blind Guardrail:** If Locust fails to return data for 15 seconds, abort immediately.

### 4. The Dead-Man Watchdog
- Create a standalone Python script `watchdog.py` running in its own container in the `controller/` project.
- The FastAPI controller writes a heartbeat timestamp to a shared file or SQLite every 5 seconds.
- If the heartbeat is older than 20 seconds, the Watchdog immediately connects to the Docker socket, issues `docker start` to all sandbox containers, clears all Toxiproxy toxics, and marks intents as aborted.

### 5. Day 1 Demo Script (`demo_day1.sh`)
You own the script that proves the platform works for the Day 1 judge checkpoint.
- Write a bash script that sequentially runs:
  1. `make up`
  2. `make map` (generates graph.json)
  3. `make baseline` (captures bands)
  4. `make inject fault=container_crash target=ratings` (Shows graceful degradation on the UI without tripping the kill switch).
  5. `make inject fault=container_crash target=cart` (Critical failure).
  6. Wait for the Kill Switch to fire and recover the system automatically.
  7. `make report`
