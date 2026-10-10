# M4 Integration Test & Teammate Execution Guide

This document outlines the step-by-step procedure for the teammate running **Stan's Robot Shop** (`instana/robot-shop`) and **Docker** to verify the end-to-end telemetry, steady-state baseline capture, Kill Switch, and Dead-Man Watchdog.

---

## 1. Network & Prerequisites Setup

The target application runs on the teammate's machine. The controller connects over the network.

### Required Open Ports on Teammate's Laptop
| Port | Service | Purpose | Test Command |
| :--- | :--- | :--- | :--- |
| **8080** | Robot Shop Web UI | Storefront | `curl http://<teammate-ip>:8080/` |
| **8089** | Locust Loadgen API | Real telemetry ingestion | `curl http://<teammate-ip>:8089/stats/requests` |
| **8474** | Toxiproxy HTTP API | Network chaos manipulation | `curl http://<teammate-ip>:8474/proxies` |
| **2375** | Docker Engine API | Remote container management (if remote) | `curl http://<teammate-ip>:2375/version` |

### Environment Variables
Configure these variables on the machine running the Controller:
```bash
export LOCUST_URL="http://<teammate-ip>:8089"
export TOXIPROXY_URL="http://<teammate-ip>:8474"
export DOCKER_HOST="tcp://<teammate-ip>:2375" # or local socket if running on same host
export SANDBOX_PROJECT="robot-shop"
```

---

## 2. Real Integration Test Procedure

### Test 1: Verify Real Telemetry Ingestion (No Faking)
Confirm Locust is actively generating load against Robot Shop:
```bash
curl -s "${LOCUST_URL}/stats/requests" | grep -o '"state":"running"'
```
If this fails or returns connection refused, **do not proceed**—the target app load container must be running first.

### Test 2: Capture Steady-State Baseline (`make baseline`)
Run the baseline capture pipeline:
```bash
# Full 10-minute steady-state run (600s, 5s interval):
python controller/baseline.py --duration 600 --interval 5 --locust-url "${LOCUST_URL}"

# Quick 30-second verification mode:
python controller/baseline.py --duration 30 --interval 3 --locust-url "${LOCUST_URL}"
```

**Verification Checklist**:
- [ ] Inspect generated `baseline.json`:
  ```json
  {
    "sample_count": 120,
    "p95_latency_ms": { "mean": ..., "upper_bound": ... },
    "error_rate": { "mean": ..., "upper_bound": ... },
    "kill_switch_thresholds": { "max_error_rate": 0.05, "p95_latency_threshold_ms": ... }
  }
  ```
- [ ] Verify non-zero request count (`sample_count > 0`).

---

### Test 3: Validate the Kill Switch (2-Window Consecutive Breach Rule)
1. Start the Controller:
   ```bash
   cd controller
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```
2. Verify Safety Pre-flight Status:
   ```bash
   curl -s http://localhost:8000/api/v1/safety/status
   # Must return "is_safe_to_inject": true, "kill_switch_state": "IDLE" or "ARMED"
   ```
3. Test Graceful Degradation (Non-critical):
   ```bash
   docker stop robot-shop-ratings-1
   # Observe Robot Shop UI: ratings component gracefully degrades without crashing storefront.
   # Check controller safety status: Kill Switch remains ARMED (does not trip).
   ```
4. Test Critical Tier-1 Failure (Kill Switch trip):
   ```bash
   docker stop robot-shop-cart-1
   # Error rate spikes > 5% on Locust.
   # Window 1 (5s): Controller enters WARNING_WINDOW_1.
   # Window 2 (10s): 2nd consecutive breach detected -> Kill Switch TRIPS!
   # Verify cart container is automatically restarted:
   docker ps | grep cart
   ```

---

### Test 4: Validate Dead-Man Watchdog
1. With Controller running, start Watchdog:
   ```bash
   python controller/watchdog.py --daemon
   ```
2. Verify heartbeat logging:
   `[INFO] watchdog: Heartbeat healthy (age: 1.2s)`
3. Simulate Controller crash or freeze:
   ```bash
   # Terminate FastAPI controller process
   kill -9 <controller-pid>
   ```
4. Wait 20 seconds:
   - At $T=20\text{s}$, Watchdog detects heartbeat staleness.
   - Watchdog triggers emergency recovery: runs `docker start` on any stopped sandbox containers, purges Toxiproxy toxics, and updates SQLite intents to `aborted`.
   - Verify all sandbox containers are running.

---

### Test 5: End-to-End Judge Demonstration
Run the automated demonstration script:
```bash
bash demo_day1.sh
```
Follow the console output to verify all 7 demo steps complete cleanly for the Day 1 judge checkpoint.
