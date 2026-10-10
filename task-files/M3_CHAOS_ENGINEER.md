# M3 - Chaos Engineer Tasks

**Role Objective:** Develop the actual mechanisms to inject and roll back failures safely within the local Docker constraints for the target app (`robot-shop`).

## Instructions for AGY CLI
Use these instructions to scaffold the failure injection tools.

### 1. Async Docker SDK Wrappers
The FastAPI controller is asynchronous, but the Python Docker SDK (`docker` module) is synchronous.
- Write an async wrapper class for all Docker API calls using `asyncio.get_event_loop().run_in_executor()`.
- This ensures operations like `docker stop` don't freeze the controller's safety and telemetry loops.

### 2. Implement the Injectors
Build Python fault executors in a `faults/` directory for the following:
- **Container Crash:** `docker stop <container>` or `docker kill`. (e.g., target `ratings` to test graceful degradation, or `cart` to test critical failure).
- **Network Latency/Loss:** Call the Toxiproxy API to inject toxics (latency, jitter) on the edges defined by M1 (e.g., latency to `rabbitmq`).
- **Disk Stress (tmpfs Fill):** 
  - M1 configured `tmpfs` mounts (e.g., 128MB for `mysql` and `mongodb`).
  - Write an injector that uses `docker exec` to run `dd if=/dev/zero of=/var/lib/mysql/chaos_fill bs=1M count=120` to simulate disk exhaustion.
  - Verify that this does *not* leak onto the host disk.
- **Message Queue Block:** Inject faults targeting `rabbitmq` to observe shipping delays.

### 3. Intent, TTLs, and Pre-flight Verification
- **Intent Logging:** Write a pending record to the SQLite database before issuing any fault command, specifying the `rollback_command` and `expires_at`.
- **Tool-Level TTLs:** Don't rely solely on Python. Set Toxiproxy toxics to auto-expire, and ensure the Watchdog knows when to forcibly restart a crashed container.
- **Pre-flight Check:** Verify with M4's observability module that the system is currently inside the steady-state baseline bands before injecting.

### 4. Rollback Verification
- Provide an explicit `rollback()` method for every fault class.
- After rolling back, the code must explicitly assert success:
  - Check Toxiproxy to ensure toxics are deleted.
  - Use Docker SDK to assert the container is `running`.
- Log the rollback success/failure back to the SQLite journal.
