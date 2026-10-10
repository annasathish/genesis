# AI-Driven Chaos Engineering Platform - MASTER PLAN

## 1. Project Overview & Objective
Build an autonomous AI system that intelligently injects controlled failures into a local sandbox environment, observes the impact, and provides actionable resilience scores.

**Target Environment Constraints:**
- Windows Host (WSL2 Docker Desktop), 16GB RAM, RTX 3050. Only ~5.2GB free RAM available.
- Absolutely NO attacks directed at real or production systems. Nothing escapes the local sandbox.

## 2. Architecture & Tech Stack

### Core Services (Controller Project)
- **Backend & Orchestration:** Python, FastAPI, SQLAlchemy + Alembic (for SQLite schema migrations).
- **State Management:** SQLite (strictly NO Redis for internal platform state to avoid self-sabotaging).
- **AI Decision Engine:** Code-based heuristic planner as the **primary path** (`--no-llm`), with Gemini 2.5 Flash API as an optional enhancement for re-ranking.
- **Safety Watchdog:** Small Python container that monitors the controller's heartbeat and performs emergency rollbacks (Dead-man switch).
- **Automation:** `Makefile` orchestrates all commands (`make up`, `make map`, `make baseline`, `make inject`, `make stop`, `make report`).

### Target Environment (Sandbox Project)
- **Primary Target Application:** **Stan's Robot Shop** (`instana/robot-shop`). 
  - *Why:* Visually impressive storefront, 11 services (Java, Node, Python, Go, PHP), 4 state stores (MySQL, Mongo, Redis, RabbitMQ). Perfectly demonstrates graceful degradation.
  - *RAM:* ~2 GB.
- **Fallback Target Application:** **Cisco Martian Bank** (`cisco-open/martian-bank-demo`).
  - *Why:* If the laptop starts swapping/lagging, pivot immediately to this ultra-light 6-service Node/Python app (RAM: ~600MB).
- **Telemetry & Load:** Robot Shop's built-in Locust loadgen (or our own pointing at the `web` service), Docker SDK (container health).
- **Fault Injection:** Docker SDK (crash), Toxiproxy (network manipulation), `tmpfs` volume mounts (disk stress).

*Critical Note:* The Chaos Controller and the Target Application must run in **completely separate `docker-compose` projects**.

## 3. The 5-Person Team & Roles
This project is divided into 5 parallel tracks. Each team member will use the Antigravity (AGY) CLI to execute their specific Markdown instructions.

- **M1 - Platform Lead:** Infrastructure, Sandbox setup (`robot-shop`), custom Compose overrides, Toxiproxy wiring, and memory capping.
- **M2 - AI & Backend Lead:** FastAPI command center, SQLAlchemy/Alembic state, Automated Dependency Mapper (`make map`), Heuristic generation, and optional Gemini re-ranking.
- **M3 - Chaos Engineer:** Execution of specific failure types, Toxiproxy toxics, tmpfs disk stress verification, and rollback functions.
- **M4 - Safety & Observability:** Telemetry ingestion, Baseline capture (`make baseline`), the Kill Switch, Dead-man Watchdog, and Day 1 Demo Script.
- **M5 - Reporting Integration:** Analysis, normalized resilience scoring, and JSON/PDF report generation (`make report`).

## 4. Addressing The Critical Constraints & Blockers
1. **Target App Switch:** Use `robot-shop` for visual impact, with `martian-bank-demo` ready as a hot-standby fallback.
2. **Model/Heuristics:** Build the heuristic code-based planner first (`--no-llm` is primary).
3. **Database Setup:** Use SQLAlchemy + Alembic for robust schema management.
4. **Dependency Mapper:** M2 builds an automated mapper parsing `docker-compose` and `docker inspect` to generate `graph.json`.
5. **Baseline Capture:** M4 implements a strict `make baseline` target (10 mins steady-state).
6. **Dead-Man Switch:** Implement a watchdog container and orphaned intent rollback on startup.
7. **Kill Switch:** Require 2 consecutive 5s windows of relative threshold breach (p95 > 5x baseline).
8. **Docker Async:** Wrap Docker SDK calls in `run_in_executor`.
9. **Disk Stress:** Mount a size-capped `tmpfs` (e.g., 64MB for MySQL/Mongo) and use `dd` to fill only that mount.
10. **Isolation:** Keep controller in a separate compose project from the target sandbox.
11. **Adaptive Loop:** Ramp magnitude (e.g., latency 200 -> 800 -> 2000 ms) until it breaks.
12. **Scoring:** Normalize formula to [0, 1] bounds.

## 5. Hackathon Timeline
### Day 1: Foundation & Safety
- **Hour 0-1:** M1 sets up Sandbox (`robot-shop`). M4 implements `make baseline`. M2 sets up FastAPI + Alembic.
- **Hour 1-3:** M1 configures Toxiproxy. M2 builds Dependency Mapper (`make map`). M3 builds core injectors. M4 builds Kill Switch.
- **Hour 3-5:** M4 integrates Watchdog. M2 tests heuristic planner. M3 tests rollbacks.
- **Hour 5-6:** M4 finalizes `demo_day1.sh`. Full integration test: `make map` -> `make baseline` -> `make inject` -> Kill Switch fires -> `make stop`.

### Day 2: Advanced AI & Reporting
- **Hour 0-2:** M2 adds Gemini LLM re-rank (optional). M3 adds DB overload / Message Queue injectors.
- **Hour 2-4:** Run 3+ distinct experiment types. M5 normalizes scores and finalizes JSON/Markdown report schema (`make report`).
- **Hour 4-5 (T-4h):** Feature Freeze. M1 triggers a clean run from a fresh clone.
- **Hour 5-6 (T-3h):** Record Demo Video. Finalize deliverable list (tools, models).
- **Hour 7 (T-30m):** Final Submission.
