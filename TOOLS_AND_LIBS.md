# Tools and libraries used

Running list for the team form. Add a row whenever you add a dependency (owner = who added it).

## Target applications

| Name | Version / pin | Licence | Use | Owner |
| --- | --- | --- | --- | --- |
| [Stan's Robot Shop](https://github.com/instana/robot-shop) | commit 55292e2, images `robotshop/rs-*:2.1.0` | Apache-2.0 | Primary chaos target (`sandbox/`) | M1 |
| [Martian Bank](https://github.com/cisco-open/martian-bank-demo) | commit 8986cf0 | BSD-3-Clause | Hot-standby target (`sandbox-backup/`) | M1 |

## Infrastructure

| Name | Version / pin | Licence | Use | Owner |
| --- | --- | --- | --- | --- |
| Docker Desktop + Docker Compose | Compose >= 2.24.4 | Docker subscription terms / Apache-2.0 | Run both projects | M1 |
| WSL2 | - | - | Docker Desktop backend, capped via `.wslconfig` | M1 |
| [Toxiproxy](https://github.com/Shopify/toxiproxy) | 2.9.0 | MIT | Network faults on 9 service edges | M1 |
| [Locust](https://locust.io) | 2.31.8 | MIT | Steady load + end-user SLIs (`/stats/requests`) | M1 |
| nginx (alpine) | 1.27 | BSD-2-Clause | Local stand-in for the external payment gateway | M1 |
| MongoDB, MySQL, Redis, RabbitMQ | as shipped by Robot Shop (mongo 5, mysql 5.7, redis 6.2, rabbitmq 3.8) | various OSS | App state stores | M1 |
| GNU Make | any | GPL-3.0 | Orchestration (`Makefile`) | M1 |

## Python (controller)

| Package | Licence | Use | Owner |
| --- | --- | --- | --- |
| fastapi 0.120, uvicorn | MIT, BSD-3 | Controller API + dashboard server | M2 |
| SQLAlchemy, Alembic | MIT | SQLite state + migrations | M2 |
| PyYAML | MIT | Compose parsing (mapper) | M2 |
| networkx | BSD-3 | Dependency graph | M2 |
| google-genai 2.x | Apache-2.0 | Google's supported Gemini SDK (`controller/ai_client.py` fallback; the primary path is the stdlib REST client `controller/insights/llm.py`) | M2 |
| pydantic | MIT | API models | M2 |
| docker (SDK) | Apache-2.0 | Fault injection, health | M3/M4 |
| httpx | BSD-3 | Toxiproxy / Locust / RabbitMQ APIs | M3/M4 |
| jinja2, typer | BSD-3, MIT | Report template, CLI | M5 |
| Python stdlib (`urllib`, `hmac`, `statistics`, `math`) | PSF | Gemini REST client, PII pseudonymization, significance tests | Insights |

## Dashboard (browser)

| Library | Version | Licence | Use | Owner |
| --- | --- | --- | --- | --- |
| [Chart.js](https://www.chartjs.org) | 4.4.1 (cdnjs) | MIT | Live, trend, timeline and cost charts on `:8050` | Insights |

## AI models

| Model | Provider | Use | Owner |
| --- | --- | --- | --- |
| `gemini-flash-latest` (Google's alias for the current Flash model; served as gemini-3.8-flash, with automatic fallback to the newest available `gemini-<version>-flash`, e.g. gemini-3.6-flash) | Google AI Studio (Gemini API) | Multi-agent findings report (Planner, Analyzer, Recommender agents), Resilience Copilot chat, per-run AI briefs, M5 analyzer explanations, M2 planner re-rank. Every agent has a rule-based fallback, and AI text is only accepted if every number in it appears in the measured data | Insights / M2 / M5 |

`gemini-2.5-flash` (the original choice) is closed to new API keys, so it is not used.

## Data used

Synthetic only: Robot Shop's bundled product catalogue and cities dataset, Locust-generated traffic. No real user data.
