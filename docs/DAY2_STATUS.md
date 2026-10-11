# Day 2: milestones, deliverables, and alignment with the team's pushes

Status of `m1/platform` against the Day-2 slides. Checked against `origin/main` (5108aa4) and
`origin/m5-reporting` (6c0a83c). Nothing from those branches is merged into `m1/platform` yet.

## Milestone 2 (Day 2)

| Milestone | Expected outcome | Where | Status |
| --- | --- | --- | --- |
| AI experiment planner | AI ranks and selects experiments using the dependency map, a short reason each | `controller/agents/planner.py`, dashboard **AI report** tab, `python -m agents plan` | Done. Executable candidates only; the 3 Day-2 experiments were its picks |
| Autonomous injection | The AI selects and injects without a human picking experiments | `controller/insights/dashboard/control.py` (Campaign), `scripts/autopilot.py`, dashboard **Start autopilot** | Done: 4-experiment campaign ran unattended (4/4 detected, rolled back, recovered; report written automatically) |
| Multiple experiments | 3+ failure types run; errors, slowdowns, recovery time recorded | `runs/*.json` (7 runs, 4 experiments: crash, latency, queue block) | Done |
| Analysis and scoring | AI explains findings, ranks weaknesses by severity, recommends fixes, resilience score | `controller/agents/analyzer.py`, `recommender.py`, `reports/FINDINGS_REPORT.md` | Done (6.38/10 overall) |
| Feature freeze and polish | No new features; fix bugs, finalize README, clean run from scratch | README (merged and aligned) | Done locally; clean-copy test run passes |
| Demo video and documents | Demo <= 3 min; architecture overview and findings report | `DEMO_SCRIPT.md`, `ARCHITECTURE.md`, `FINDINGS_REPORT.md` (aligned) | **Open**: record the video |
| Submission | Team form, verify all five deliverables open, submit 30 min early | `TOOLS_AND_LIBS.md` (tools, AI models, libraries, data) | **Open** |

## Five deliverables

| # | Deliverable | Source of truth | Note |
| --- | --- | --- | --- |
| 1 | Source code + README (setup, how to run) | repo; README on `main` | README commands `make app-up`, `make setup`, `make ai-up` do not exist (real: `make pull`, `make up`, `make verify`, `make dashboard`, `make findings`) |
| 2 | Demo video (<= 3 min: inject, observe, report) | to record | `DEMO_SCRIPT.md` on `main` points at `localhost:8000/dashboard` and quotes fixed figures ("$5,000 revenue risk, 15% bounce rate"); use `:8050` and the generated numbers |
| 3 | Architecture overview (components, how the AI decides, safety) | `ARCHITECTURE.md` on `main` | should name the three agents and the grounding check |
| 4 | Findings report (weaknesses ranked, with fixes) | **`reports/FINDINGS_REPORT.md`** (generated from real runs) | see the issue with `FINDINGS_REPORT.md` on `main` below |
| 5 | Team form (team, tools, AI models, libraries, data) | `TOOLS_AND_LIBS.md` | updated: Gemini flash via REST, Chart.js, stdlib |

## Optional stretch goals

| Goal | Status |
| --- | --- |
| Re-run experiments after a fix | **Done**: catalogue x2 replicas (`scripts/apply_fix.py`), re-run twice: error-minutes -98.9 %, p = 2.8e-08, plus a measured side effect (failover hang). Shown on the Experiments tab and in the report's "Fix verification". Sandbox reverted to 1 replica afterwards. `apply_resilience_fix.sh` (on `main`) scales **shipping**, which none of our experiments target |
| Live dashboard / simple UI | Done: `make dashboard` -> http://127.0.0.1:8050 (Live, Experiments, Business impact, AI report, Copilot) |
| Combined failures | `/api/v1/inject/combined` on `main` cannot work as pushed (below); fix is on branch `backup/merge-main-c22e535` |

## Merge of `origin/main` (12 commits, through 4c89386) on 2026-10-11

Reviewed commit by commit, merged locally, verified in the controller's own image (Python 3.11).

Already fixed by the team (thank you):

| Earlier issue | Fixed in |
| --- | --- |
| `/api/v1/inject/combined` called a missing `schedule_and_inject` | 130f959 / 4c89386: uses `execute_fault`, distinct-target check, safety gate |
| Planner proposed unexecutable targets, Gemini SDK path never worked | 4c89386: injector-allowlisted candidates, Toxiproxy links from the mapper, `google-genai` via `ai_client.py` |
| `dashboard_api.py` random fallbacks | 130f959 / 4c89386: real telemetry, revenue labelled as an illustrative proxy |
| Kill switch / heartbeat never started, DB paths split (M1 issues 1, 3) | 130f959: started in the API lifespan, `CHAOS_DB_URL` everywhere |
| `resolve_container` could hit the wrong project (M1 issue 6b) | 130f959: project + target-label filters |
| `experiment_result` never written, so `report_gen` had 0 experiments | 130f959: rollback writes `ExperimentResult` |
| M5 CLI routing (`cli.py report`) | 4087b1f |
| README `make app-up / setup / ai-up` | 4c89386 |

Fixed while merging (problems that existed on `main` itself):

| Problem | Effect | Fix |
| --- | --- | --- |
| `requirements.txt`: `fastapi==0.103.1` needs `anyio<4`, `google-genai 2.x` needs `anyio>=4.8` | `pip install` / image build fail (ResolutionImpossible) | `fastapi==0.120.0` (verified: image builds, `pip check` clean) |
| `typer==0.9.0` with an unpinned `click` (8.5) | every `cli.py` call exits 2, `--help` exits 1: `make report` broken, also on the running controller | `click>=8.0,<8.2` |
| New model columns (`rmq_depth`, `target_service`, `details`) | an existing `chaos_state.db` fails with "no such column" (reproduced on a copy of ours) | `models.ensure_columns()`: adds missing nullable columns, idempotent, called by `main.py` and the reporter |
| `ai_client.py`: fixed `gemini-3.8-flash`, no timeout | that model hung 60 s+ in our tests; dashboard calls could block | same interface, routed through `insights/llm.py` (timeout, fallback, quota handling); `google-genai` SDK kept as fallback |
| `/api/v1/inject` now takes a JSON body | `scripts/day1_demo.py` would get HTTP 422 | sends the JSON body (falls back to the old query form), explains a 409 from the safety gate |
| `/api/v1/intents/active` removed | the dashboard's Live tab lost active faults | restored as a read-only route |
| `/dashboard` served the old page again | team decision: one dashboard | redirect registered before `dashboard_api`'s router; its JSON routes stay; its Judge Console is ported as the Live tab's **Experiment console** (approval, safety gate, emergency stop) |
| `FINDINGS_REPORT.md` described runs that never happened | deliverable 4 | replaced with the report generated from `runs/` |

Kept as the team wrote it after checking: per-request `ChaosManager` (a sleeping TTL task is kept alive by the event
loop: verified), the new kill switch and safety gate, the mapper, the planner, `day2_execute.py`, `.env.example`.

Notes: the Makefile now uses `PYTHON ?= python3` and the repo-root `.env`; this laptop has no `make` (every target is
a plain command). The controller must be restarted (`make controller-up`, which rebuilds) to run the merged code.

## Robot Shop refresh bug (fixed, M1)

The shop's AngularJS app uses html5Mode, so `/cart`, `/login`, `/product/...` are client-side routes; nginx had no
SPA fallback, so a browser refresh on any of them returned 404. `sandbox/web/default.conf.template` (upstream template
+ `try_files $uri $uri/ /index.html;`) is mounted by `sandbox/docker-compose.override.yml`. Every page now survives a
refresh; API routes are unaffected.
