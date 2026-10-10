# AI-Driven Chaos Engineering Platform

## Team Genesis - Bosch Hackathon Submission

This repository contains our Autonomous, Resource-Aware Chaos Engineering platform. Built specifically to operate under strict hardware constraints (< 5.2GB RAM), it uses a two-tiered AI system (Python Heuristics + Gemini 2.5 Flash) to map microservice dependencies, rank vulnerabilities, and inject targeted faults into a cloud-native environment.

### Stretch Goals Achieved
- ✅ **Live Dashboard:** Real-time UI visualizing system health, vulnerability rankings, and business ROI impact.
- ✅ **Combined Failures:** Capable of triggering simultaneous faults (e.g., Network Latency + Container Crash).

---

## 1. Prerequisites
- Docker & Docker Compose (OrbStack recommended for Mac/M1; Native WSL2 for Windows).
- Python 3.10+
- A valid Gemini API Key.

## 2. Setup & Clean Run Instructions

To guarantee a clean run from scratch, open your terminal and follow these steps:

### Step 1: Start the Target Environment
We use Stan's Robot Shop (a microservice e-commerce app) injected with Toxiproxy.
```bash
git clone https://github.com/annasathish/genesis.git
cd genesis
make sandbox-up
```
*Wait 2-3 minutes for the 11 containers to fully initialize.*

### Step 2: Initialize the AI Controller
In a second terminal window:
```bash
export USE_LLM="true"
export GEMINI_API_KEY="your_actual_api_key_here"

# Install dependencies and start the FastAPI server
make pull
make controller-up
```

### Step 3: View the Live Dashboard
Open your web browser and navigate to:
**[http://localhost:8000/dashboard](http://localhost:8000/dashboard)**
Keep this open to watch the AI orchestrate attacks in real-time.

### Step 4: Execute the Chaos (Day 1 & Day 2 Demos)
In a third terminal window, trigger the automated experiments:
```bash
# Run the standard 3-fault sequence
make demo

# (Optional) Trigger a Combined Failure Stretch Goal
curl -X POST "http://localhost:8000/api/v1/inject?fault_type=network_latency&target=shipping" & 
curl -X POST "http://localhost:8000/api/v1/inject?fault_type=container_crash&target=redis"
```

## 3. Architecture & Safety
Our platform operates on a closed OODA loop. It statically maps the `docker-compose.yaml` (eliminating AI hallucinations), uses Gemini to select the optimal blast radius, executes via the Docker SDK, and monitors the fallout via Locust.

**Safety Controls:** A background Watchdog continuously polls system memory and error rates. If the host laptop exceeds 95% RAM utilization or the application hits 100% error rate, the Kill Switch instantly aborts all experiments and rolls back the containers.
