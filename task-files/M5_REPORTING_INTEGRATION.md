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

# M5 - Reporting Integration Tasks

**Role Objective:** Translate raw experiment data from SQLite into normalized resilience scores and generate the final structured reports.

## Instructions for AGY CLI
Use these instructions to scaffold the reporting module.

### 1. Normalized Resilience Score
The scoring formula must be normalized to a strictly `[0, 1]` scale before scaling to 10.
- **Latency Penalty:** e.g., `latency_score = max(0, 1 - min((p95_ratio - 1) / 4, 1))`.
- **Error Penalty:** e.g., `error_score = max(0, 1 - (error_rate / max_tolerable_error))`.
- **Recovery Bonus:** Faster recovery time (time from rollback until metrics stabilize in baseline for 3 windows) = closer to 1.
- **Tool Metrics:** Do not add bonus points to the app score if the Kill Switch fired; log it separately as a Platform Safety metric.

### 2. LLM Report Generation (Strict Constraints)
- When generating the post-experiment analysis using Gemini, enforce strict constraints.
- The LLM must NEVER hallucinate or write numeric metrics.
- Python code must extract metrics, windows, and recovery times from the SQLAlchemy journal and inject them into a Jinja2 Markdown template (`template.md.j2`).
- The LLM only provides an explanation and selects a fix from a fixed vocabulary list (e.g., `[Increase Replicas, Adjust Timeouts, Add Retries, Implement Circuit Breaker]`).

### 3. Final Report & Aggregation (`make report`)
- Aggregate the results of all experiments in the database.
- Generate an overall ranked list of application weaknesses sorted by severity/criticality.
- Implement a Typer CLI command (`make report` -> `python cli.py report`) that reads the SQLite DB and outputs `report.json` and `REPORT.md`.

### 4. Integration Testing
- Ensure the reporting module can pull directly from the SQLite database left by M2 and M4 without requiring the controller FastAPI server to be actively running.
- This ensures the report can be generated flawlessly in the final T-30 minute window.
