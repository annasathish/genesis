# ChaosPilot: AI-Guided Resilience Testing

Team Genesis · Bosch Hackathon

ChaosPilot maps a local microservice sandbox, ranks failure experiments, injects a reviewed fault, observes Locust and Docker telemetry, and automatically rolls the experiment back when safety thresholds are crossed. Gemini explains the experiment and recommends a resilience fix; deterministic planning and telemetry-grounded guidance remain available if the model is unavailable.

## Run on the demo laptop

Requirements: Docker Desktop or OrbStack with Compose, Python 3.10+ for scripts and tests, and a Gemini API key for AI explanations.

1. Keep the API key in the demo laptop's ignored root `.env` file. If the file does not exist, copy `.env.example` to `.env` and add the key. Never commit `.env`.
2. Pull the sandbox images and start the full stack:

   ```bash
   make pull
   make up
   ```

3. Wait for the Robot Shop services and controller to become healthy, then start the dashboard (host Python with
   `fastapi` + `uvicorn`) and open [http://127.0.0.1:8050](http://127.0.0.1:8050)
   (`http://localhost:8000/dashboard` redirects there):

   ```bash
   make dashboard          # or: cd controller && python -m insights.dashboard.server
   ```
4. Capture the baseline and run the guided demo:

   ```bash
   make demo
   ```

   The standard baseline takes 10 minutes. For a short rehearsal, use `TEST_MODE=true make demo`; this captures a 10-second baseline.

The dashboard's **Experiment console** (Live tab) shows the planner's top experiment and its reason, checks the controller's safety gate, and waits for a human approval before running it as a measured experiment (automatic rollback, 180-second TTL backstop). The **Emergency stop** button trips the kill switch and rolls back active sandbox experiments. The other tabs show run-to-run improvement with significance tests, business impact, and the **AI report**: a Planner → Analyzer → Recommender multi-agent findings report (`make findings` from the terminal). The API also supports a ranked experiment plan, bounded TTL injection, combined faults on distinct services, and a findings report.

To measure one experiment end to end (baseline, inject, detect, rollback, recovery) and record it for the dashboard: `python scripts/day1_demo.py --fault network_latency --target payment_to_gateway --params '{"latency_ms": 2000}'`. To show an improvement, apply a fix (`make fix-replicas svc=catalogue n=2`), re-run the same experiment with `--label "fix: ..."`, then `make fix-revert svc=catalogue`.

For a longer Day 2 run, start from the same baseline and use `python3 day2_execute.py`. It runs varied, 30-second experiments one at a time, tries one combined pair on distinct services, then generates the report. Keep the controller and sandbox running while it completes.

The controller only targets containers labeled as part of the `robotshop` sandbox and uses the separate controller Compose project. It requires a baseline plus live Locust and Docker telemetry before accepting an injection. Experiments have a 1–300 second TTL, and the kill switch applies its own rollback path if telemetry crosses the configured limits.

## AI and business impact

Set `GEMINI_API_KEY` and `USE_LLM=true` in the ignored root `.env` file (the dashboard also reads it, no restart needed). Gemini calls go through one client (`controller/insights/llm.py`, used by `controller/ai_client.py`) with a timeout and automatic fallback to the newest available Flash model. The planner uses the model to rerank dependency-based candidates; if the model call fails, it uses the deterministic heuristic order. The multi-agent report accepts AI text only if every number in it appears in the measured data; personal data is masked before any model call. Dashboard recommendations use observed sandbox error and latency. Revenue is labeled as an illustrative proxy based on an explicit $100/minute assumption; the sandbox does not measure production revenue, DAU, churn, or support volume.

## Development checks

Install runtime and test dependencies, then run both test suites:

```bash
python3 -m pip install -r controller/requirements-dev.txt
make test
```

`make down` stops the controller and sandbox while preserving volumes. `make nuke` also deletes volumes and stored experiment data.

## Architecture

- **Map and plan:** parse the Robot Shop Compose file and configured Toxiproxy links, generate only supported injection targets, rank them using dependency roles, and optionally rerank with Gemini.
- **Inject:** call the fault-specific injector only for approved target-labeled sandbox services; persist an experiment intent and TTL before returning.
- **Observe:** sample Locust request errors and p95 latency alongside Docker container health, persisting the measurements for the dashboard and report.
- **Protect:** require a captured baseline and live telemetry; confirm breaches across consecutive windows; call the injector's rollback and then run scoped container/Toxiproxy recovery.
- **Explain:** compare observed telemetry with the experiment hypothesis, show the measured result, and recommend a concrete resilience change to test again.
- **Report:** three agents (Planner, Analyzer, Recommender) turn the measured runs into `reports/FINDINGS_REPORT.md`: weaknesses ranked by severity, a resilience score, prioritised fixes with the command to verify each, and before/after-fix verification. Method: `docs/INSIGHTS.md`.

Everything runs inside the team's local sandbox. Real or production systems, shared infrastructure, and other participants' environments are out of scope.
