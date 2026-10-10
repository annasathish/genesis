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

# M1 - Platform Lead Tasks

**Role Objective:** Set up the isolated sandbox environment using **Stan's Robot Shop** (with Martian Bank as a backup), ensure system limits are respected to prevent crashing the 5.2GB free RAM host, and wire up the network routing for fault injection.

## Instructions for AGY CLI
Use these instructions to scaffold the infrastructure code.

### 1. Primary Target: `instana/robot-shop`
We are using Robot Shop to demonstrate 11-service complexity and graceful degradation.
- Clone `https://github.com/instana/robot-shop`.
- It comes with a complete `docker-compose.yaml`. Place it in the `sandbox/` directory.
- Add a `docker-compose.override.yml` to apply our chaos-specific overrides (tmpfs limits, Toxiproxy).

### 2. Backup Target (Hot Standby): `martian-bank-demo`
If Robot Shop proves too heavy during Hour 1 and the laptop starts paging/lagging, pivot immediately to the backup.
- Keep a secondary folder `sandbox-backup/` containing `cisco-open/martian-bank-demo`.
- It is ultra-lightweight (~600MB) Node.js/Python and guarantees survival.

### 3. Configure WSL2 Memory & tmpfs Caps
- **WSL Config:** Write a `.wslconfig` file for the Windows host (`%USERPROFILE%/.wslconfig`) to strictly cap WSL memory at 6GB. Provide a PowerShell script to create this and run `wsl --shutdown`.
- **tmpfs Overrides:** To safely test disk exhaustion without filling the Windows host disk, mount size-capped `tmpfs` volumes for the databases in `docker-compose.override.yml`:
  ```yaml
  services:
    mysql:
      tmpfs:
        - /var/lib/mysql:size=128M,mode=1777
    mongodb:
      tmpfs:
        - /data/db:size=128M,mode=1777
  ```
- Write a bash test (`docker exec <container> df -h`) to verify the caps are strictly applied.

### 4. Wire Up Toxiproxy
To inject network latency or drop packets, Toxiproxy must sit between the services.
- Add a `toxiproxy` service to the `sandbox/docker-compose.override.yml` (image: `ghcr.io/shopify/toxiproxy`).
- Map specific critical service connections through Toxiproxy. For example, `shipping` talking to `rabbitmq`:
  - Toxiproxy listens on `15672` and forwards to `rabbitmq:5672`.
  - The `shipping` app environment variable must point to `toxiproxy:15672`.

### 5. Setup Load Generation
- Robot Shop includes a `load` container. Ensure it is running to generate steady background traffic.

### 6. Controller Isolation & Makefile
- Create a completely separate directory `controller/` with its own `docker-compose.yml` for the FastAPI backend and Watchdog.
- Create a top-level `Makefile` to orchestrate everything:
  - `make up` (starts sandbox and controller)
  - `make down`
- Maintain a `TOOLS_AND_LIBS.md` file listing all tools and libraries used.
