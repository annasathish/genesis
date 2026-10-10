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

3. Wait for the Robot Shop services and controller to become healthy. Open [http://localhost:8000/dashboard](http://localhost:8000/dashboard).
4. Capture the baseline and run the guided demo:

   ```bash
   make demo
   ```

   The standard baseline takes 10 minutes. For a short rehearsal, use `TEST_MODE=true make demo`; this captures a 10-second baseline.

The dashboard's **Judge Console** previews the planner's top experiment, shows its reason and failure hypothesis, and waits for a human approval before injecting a 30-second fault. The **Emergency stop** button requests rollback for active sandbox experiments. The API also supports a ranked experiment plan, bounded TTL injection, combined faults on distinct services, and a findings report.

For a longer Day 2 run, start from the same baseline and use `python3 day2_execute.py`. It runs varied, 30-second experiments one at a time, tries one combined pair on distinct services, then generates the report. Keep the controller and sandbox running while it completes.

The controller only targets containers labeled as part of the `robotshop` sandbox and uses the separate controller Compose project. It requires a baseline plus live Locust and Docker telemetry before accepting an injection. Experiments have a 1–300 second TTL, and the kill switch applies its own rollback path if telemetry crosses the configured limits.

## AI and business impact

Set `GEMINI_API_KEY` and `USE_LLM=true` in the ignored `.env` file. The planner uses the model to rerank dependency-based candidates; if the model call fails, it uses the deterministic heuristic order. Dashboard recommendations use observed sandbox error and latency. Revenue is labeled as an illustrative proxy based on an explicit $100/minute assumption; the sandbox does not measure production revenue, DAU, churn, or support volume.

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

Everything runs inside the team's local sandbox. Real or production systems, shared infrastructure, and other participants' environments are out of scope.
