# Insights: improvement, business impact, PII, dashboard + Copilot

Answers to the evaluator's four questions, and where each lives in the code.

| Question | Answer in one line | Code |
| --- | --- | --- |
| How is improvement between runs calculated? | Relative change of a defined KPI between like-for-like runs, only claimed when statistically significant | `controller/insights/compare.py` |
| How does it impact the business? | Measured checkout loss projected onto production traffic with explicit, editable assumptions | `controller/insights/business.py`, `business_config.json` |
| Is personal information masked? | Yes, internally: one masker on every path out (dashboard API, chat, LLM prompts). Not a UI feature | `controller/insights/pii.py` |
| Interactive dashboard + AI chat? | `make dashboard` → http://127.0.0.1:8050: live system map, vulnerability board, experiments, business hub, Gemini-backed Copilot | `controller/insights/dashboard/`, `live.py`, `vulnerability.py`, `chat.py`, `llm.py` |

```bash
make insights      # terminal summary
make dashboard     # http://127.0.0.1:8050  (read-only over runs/*.json; safe next to a live experiment)
python scripts/day1_demo.py --target catalogue --label "cart: cache catalogue products"   # a labelled run
```

It only reads: `runs/*.json`, plus live data from Locust, the controller API, Toxiproxy and `docker ps`. It never
injects or rolls back anything, and runs as its own process on port 8050 (the controller API stays on 8000).

### AI key (one place)

```bash
cp .env.example .env      # repo root; then put your key after GEMINI_API_KEY=
```

`.env` is gitignored. The dashboard re-reads it on every call (falling back to `controller/.env`), so the Copilot and
AI briefs switch from "offline" to Gemini without a restart. The controller container (planner re-rank, `make report
--llm`) gets `GEMINI_API_KEY` and `USE_LLM` from the same file, but only on (re)start:
`docker compose --env-file .env -f controller/docker-compose.yml up -d` (what `make controller-up` runs).

---

## 1. How improvement is calculated ("50 → 30 → 20 → 10")

**Step 1: only compare like with like.** Runs are grouped by *signature* = fault + target + parameters. A
catalogue crash is never compared with a cart latency test.

**Step 2: one primary KPI that captures both severity and duration.**

```
error_minutes = Σ (window error rate × window length) / 60        over fault + recovery windows
```

"Outage-equivalent minutes": 1 minute at 100 % errors = 2 minutes at 50 %. Peak error alone would miss
duration, and duration alone would miss severity. Secondary KPIs are shown too: peak and fault-phase error
rate, failed requests, checkout-loss minutes, time to detect, time to recover, blast radius, and the M5
resilience score (`scorer.py`, unchanged).

**Step 3: relative change, step by step and cumulative.**

```
relative_change = (old − new) / old        (lower-is-better KPIs; positive = better)
```

| Runs | Error | Step change | Cumulative vs first |
| --- | --- | --- | --- |
| 1 | 50 | – | – |
| 2 | 30 | −40 % | −40 % |
| 3 | 20 | −33 % | −60 % |
| 4 | 10 | −50 % | −80 % |

**Step 4: is it real or noise?** A step is labelled:

- **improved / regressed**: the fault-phase error rate change passes a **two-proportion z-test** (failures /
  requests, p < 0.05) *and* error-minutes moved ≥ 10 % in the *same direction*;
- **mixed**: both are significant but they disagree (e.g. a shorter outage with a higher error rate while it lasted);
- **no significant change**: anything else. Not claimed.

Detect/recover times are sampled in 5 s windows, so differences under one window are reported as "within
measurement resolution".

**Step 5: attribution.** Each run carries a `--label` naming the change made before it, so every step on the
dashboard says *what* caused it.

### What our own runs show (catalogue container crash, 3 runs)

| Step | Error-minutes | p-value | Verdict |
| --- | --- | --- | --- |
| Run 1 → 2 | 0.183 → 0.112 (−38.8 %) | 0.48 | no significant change |
| Run 2 → 3 | 0.112 → 0.150 (+33.9 %) | 0.004 | regressed |
| Run 1 → 3 | 0.183 → 0.150 (−18.0 %) | 0.023 | mixed (fault error rate rose 2.2 % → 6.3 %) |

No code changed between these runs. A naive "error went down 39 %" would have been a claim built on noise:
this is exactly why the significance test is there. To show a real improvement, apply a fix (e.g. cache
catalogue data in `cart`) and re-run the same experiment with `--label`.

## 2. Business impact

Sandbox traffic is tiny (10 Locust users), so the impact is **measured in the sandbox and projected onto
production traffic** using assumptions in `controller/insights/business_config.json` (editable live on the
dashboard).

```
checkout_loss_min    = Σ (1 − successful_checkouts_w / expected_checkouts_w) × window / 60
                       expected = baseline payment:pay success rate.  (Counting payment:pay failures would be
                       wrong: when cart:add fails the shopper never reaches payment, so lost checkouts show as 0.)
orders_lost          = checkout_loss_min × orders_per_minute
revenue_at_risk      = orders_lost × avg_order_value
revenue_lost         = revenue_at_risk × (1 − customer_retry_rate)
ops_cost             = incident_minutes × incident_cost_per_minute
cost_per_incident    = revenue_lost + ops_cost
annual_exposure      = cost_per_incident × incidents_per_year
slo_budget_burn_pct  = error_minutes / (slo_window_days × 1440 × (1 − slo_target)) × 100     (99.9 %/30 d = 43.2 min)
savings (run A → B)  = cost_per_incident(A) − cost_per_incident(B), and × incidents_per_year
```

Savings are only presented as real when the improvement verdict for the same step is "improved".

Stakeholder view per run (rules, documented in `business.py`): **churn risk** HIGH / MEDIUM / LOW from whether
the checkout flow failed and how badly; **funnel impact**, the shopper journey stages (browse, rate, login, add to cart,
checkout) whose endpoints failed; **support tickets** = lost customers × `support_ticket_rate`, priced with
`cost_per_ticket`. The AI brief on the Business tab turns these numbers into a 4-part narrative (what happened, what
it affects, time and money, recommendation); the model gets only computed numbers and may not invent any.

Runs recorded before per-endpoint capture (the first three) are flagged **est.**: their checkout loss falls back
to the aggregate error rate. `scripts/day1_demo.py` now records per-endpoint counts for every window and the
baseline checkout rate, so new runs are measured directly.

## 3. Personal data masking

`controller/insights/pii.py` (`Masker`). Two treatments:

| Data | Treatment |
| --- | --- |
| Passwords, tokens, API keys (Google/OpenAI/GitHub/AWS), JWTs, bearer headers, `password=…` | redacted |
| Payment cards (13–19 digits, Luhn-checked so counters are not hit) | redacted, last 4 kept |
| Aadhaar, PAN, SSN | redacted |
| Emails, phones (+cc, Indian mobile, NANP), IPv4 | pseudonymized |
| User ids (UUIDs: Robot Shop cart/user ids) | pseudonymized |
| JSON keys: email, phone, address, location, name parts, DOB, card, cvv, … | redacted |

Pseudonyms are HMAC-SHA256 with `PII_SALT` (set it per deployment): stable, so `user_1a2b3c` can be followed
across runs, but not reversible. `name` is deliberately not a sensitive key: run data uses it for endpoint names.

Applied at every exit, internally (deliberately not shown on the dashboard):
- dashboard API: every run file is masked on load, before any response;
- Copilot and AI briefs: the user's question, chat history and fact sheet are masked before an LLM call, and the
  answer again after;
- M5 analyzer (`controller/analyzer.py`): the Gemini prompt and its explanation are masked.

## 4. Dashboard and Resilience Copilot

`make dashboard` → http://127.0.0.1:8050 (localhost only). Three tabs, with the Copilot beside them:

- **Live** (polls every 3 s): status badge (stable / injecting / degraded / recovering, from active intents,
  Toxiproxy toxics and container health); live error rate, p95, throughput; the kill-switch rule evaluated on live
  windows with the thresholds in `controller/baseline.json`; a clickable **system map** of the real dependency graph
  coloured by live container and endpoint health, with failing edges animated; the **vulnerability board**; live
  throughput / error / p95 charts. Data-source dots in the header show what is connected. A source that is down
  shows "—", never a made-up value.
- **Experiments**: run-to-run improvement (section 1) and the per-run error timeline.
- **Business impact**: AI stakeholder brief per run, funnel stages blocked, churn risk, cost tiles, editable
  assumptions, per-run cost breakdown (revenue lost + ops + support).

**Vulnerability board** (`vulnerability.py`): `H_v = 100 × (0.4 × peak error + 0.3 × min(1, incident s / 180)
+ 0.3 × impacted services / 7)`, averaged over the runs that targeted the service. Services never tested are
**UNTESTED** (no score) and ordered by predicted blast radius from the dependency graph, i.e. what to test next.
A service flashes when its rank changes or it starts failing live.

**Copilot** (`chat.py`, `llm.py`): Gemini through REST: `gemini-flash-latest` by default (Google's alias for the current Flash model; `gemini-2.5-flash` is closed to new keys), or `CHAT_MODEL`; on HTTP 404 the newest available `gemini-<version>-flash` is discovered via ListModels. Thinking is disabled for these short answers, key in
the `x-goog-api-key` header. Grounded: Python builds a masked fact sheet (measured runs, comparisons, business
numbers, and the current live state) and the model must answer only from it. API errors are shown in plain words
("API key rejected (HTTP 400)", "rate limit"); without a key, or on error, a rule engine answers from the same facts.

### One dashboard

This is the team's only dashboard. The earlier `controller/dashboard_api.py` + `dashboard.html` (`:8000/dashboard`)
is no longer mounted in `main.py`: it filled missing data with `random.uniform(...)` and used fixed business figures.
`http://127.0.0.1:8000/dashboard` now redirects here. The files are kept in git, unused.

### Analyzer and planner use the same client

`controller/analyzer.py` (M5, `make report --llm`) and `controller/planner.py` (M2 re-rank, only when `USE_LLM=true`)
call Gemini through `insights/llm.py` too. Their old path used `genai.GenerativeModel`, which does not exist in the
pinned `google-generativeai==0.2.1`, so it always failed silently into the heuristic fallback. Both keep that
fallback for every failure (no key, model gone, model hanging, quota, bad JSON). Models are tried in order:
`CHAT_MODEL` / `gemini-flash-latest`, then the newest available `gemini-<version>-flash`; a model that 404s, hangs
for `LLM_TIMEOUT_S` (20 s) or is out of free-tier quota is skipped, and the working one is remembered per process.

## 5. Multi-agent findings report (Day 2)

`controller/agents/`: three agents run by a coordinator. **Python computes every number; agents explain, rank and
recommend.** Dashboard: **AI report** tab -> *Generate report*. CLI: `make findings` (`llm=0` for rules only).
Output: `reports/FINDINGS_REPORT.md` + `reports/findings_report.json`.

| Agent | Input | Output |
| --- | --- | --- |
| **Planner** (`planner.py`) | dependency map (`insights/live.py` EDGES, M2 `graph.json` kinds), M1 Toxiproxy edges, M3 injector targets, measured runs | executable candidates scored `100 x (0.40 revenue proximity + 0.20 blast radius + 0.25 novelty + 0.15 new fault type)`, a reason each, selection of at most 4 experiments, one per fault type |
| **Analyzer** (`analyzer.py`) | per-experiment metrics, run-to-run verdicts | findings ranked by severity `100 x (0.4 error spike + 0.3 incident time + 0.3 cascade) + slowdown`, where error spike = worse of overall errors and failed checkouts; resilience score per experiment and overall (M5 `scorer.py`) |
| **Recommender** (`recommender.py`) | Analyzer findings, business impact | one fix per weakness (M5 vocabulary + 2), priority = severity x projected annual cost, money protected, the exact command to verify the fix |

Every agent (`base.py`) computes a full rule-based answer first, then asks Gemini (JSON mode) to improve wording and
ranking. An AI field is accepted only if its ids are known, its category is allowed, and **every number in its text
appears in the agent's input** (grounding check); otherwise the rule text stays and the reason is in the report's
agent table. Gemini down, out of quota or hanging -> rule-based output, never a failed report.

### Experiments (4, 3 failure types)

| Experiment | Chosen by | Result |
| --- | --- | --- |
| `container_crash` on catalogue | day 1 | 4 runs; cascades to cart + ratings; run-to-run: no significant change |
| `network_latency` 2000 ms on `payment_to_gateway` | Planner #1 (81.7) | no errors, `payment:pay` 21.5 -> 3141.9 ms (146x), 65.6 % of checkouts did not complete |
| `container_crash` on payment | Planner #2 (66.7) | checkout hangs ~38 s (p95 38000 ms) instead of failing fast; 46.9 s to detect; 58.4 checkouts lost |
| `rabbitmq_block` on rabbitmq | Planner #3 (65.0) | every checkout fails while the queue is down: payment publishes synchronously |

### Re-run after a fix (stretch goal)

`python scripts/apply_fix.py replicas --service catalogue --count 2` (or `make fix-replicas svc=catalogue n=2`), then
`python scripts/day1_demo.py --target catalogue --label "fix: catalogue x2 replicas"`, then `apply_fix.py revert`.
Runs labelled `fix: ...` form a new phase; the Experiments tab and the report compare phases with all runs pooled.

| | Baseline, no change (4 runs) | fix: catalogue x2 replicas (2 runs) |
| --- | --- | --- |
| Error-minutes (mean) | 0.142 (noise band 0.112-0.183) | 0.0015 |
| Fault-phase failures | 39 / 994 requests | 0 / 768 requests |
| Verdict | | **improved**, -98.9 %, p = 2.8e-08 |
| Side effect | | requests routed to the crashed replica hung up to ~51 s before failover (nginx's default 60 s connect timeout), so the resilience score fell 8.35 -> 5.9: the next fix is a short gateway connect timeout |

The first four catalogue runs are repeats with nothing changed, which is why their bars do not trend: the chart
shades that spread as the run-to-run noise band.

`scripts/day1_demo.py` gained `--params` (JSON), a rolling p95, per-endpoint window latency (`lat`) and a slowdown
breach rule (endpoint mean > 5x its baseline, M4's p95 rule), `web` restarted only after crashes, and rollback on
Ctrl+C or crash.

## Tests

`controller/tests/test_insights.py` (run by `make test`): the 50→30→20→10 example, noise, regression and mixed
verdicts, z-test math, business formulas by hand, stakeholder rules, live window maths + kill-switch rule, the
H_v scoreboard, readable LLM errors and model fallback, the AI brief, measured checkout loss when payment is never reached, every PII
detector, no false positives on real run data, LLM payload masking (mocked), fallback on LLM failure, and the
dashboard API.
