# Demo runbook (Windows, cmd + browser)

Everything below was run on the demo laptop. Commands are for **Command Prompt (cmd)**; one per line. `make` is not
needed (and is not installed on this laptop): each step is the plain command a Makefile target would run.

## 0. One-time setup (already done on this laptop)

1. Docker Desktop installed and running (whale icon steady, "Engine running").
2. Python 3.10+ on PATH with the dashboard's three packages:
   ```
   pip install fastapi uvicorn pydantic
   ```
3. API key in the **ignored** root `.env` (never in `.env.example`, which is committed):
   ```
   cd /d D:\genesis
   copy .env.example .env
   notepad .env
   ```
   Put the key after `GEMINI_API_KEY=` and save. Without a key everything still works with rule-based AI.
4. Images pulled once (~2-3 GB): `docker compose -f sandbox/docker-compose.yaml -f sandbox/docker-compose.override.yml pull`

## 1. Start the system (about 3 minutes)

Open **cmd window 1**:

```
cd /d D:\genesis
docker compose -f sandbox/docker-compose.yaml -f sandbox/docker-compose.override.yml up -d --no-build --wait --wait-timeout 420
docker compose --env-file .env -f controller/docker-compose.yml up -d --build
```

The first command starts Robot Shop (15 containers) and waits until they are healthy; the second starts the AI
controller (API, kill switch, safety gate). Both are safe to re-run: running containers are left alone.

Check in the browser: http://127.0.0.1:8000/api/v1/safety/status should show `"is_safe_to_inject": true`.
If it says "Capture a steady-state baseline", run this once (60 s) and check again:

```
curl -X POST "http://127.0.0.1:8000/api/v1/baseline?duration=60"
```

Open **cmd window 2** for the dashboard and leave it running:

```
cd /d D:\genesis\controller
python -m insights.dashboard.server
```

## 2. Open the browser tabs

| Tab | URL | What it is |
| --- | --- | --- |
| Shop | http://127.0.0.1:8080 | Stan's Robot Shop, the system under test |
| Dashboard | http://127.0.0.1:8050 | our dashboard (http://127.0.0.1:8000/dashboard redirects here) |
| Load generator | http://127.0.0.1:8089 | Locust: 10 simulated shoppers, live request stats |
| Controller API | http://127.0.0.1:8000/docs | the controller's API, for questions about endpoints |

## 3. Robot Shop: log in and buy something (show it works first)

Demo accounts (Robot Shop's built-in sample users, not real people):

| Name | Password |
| --- | --- |
| `user` | `password` |
| `stan` | `bigbrain` |
| `partner-57` | `worktogether` |

1. **Login / Register** (top right) → Name `stan`, Password `bigbrain` → **Login**. The page greets you and shows
   **Order History**.
2. **Categories** (left) → **Robot** → pick any robot → **Add to cart**.
3. **Cart** (top right) → **Checkout**.
4. **Shipping information**: choose a country (e.g. United Kingdom), type 3+ letters of a city (e.g. `lon`) and pick
   one from the list → **Calculate** → **Confirm**.
5. **Review your order** → **Pay Now**. You get an order ID; it appears under **Order History** on the login page.

## 4. The demo itself (dashboard)

1. **Live tab, top banner:** "All 7 shop services are healthy". The *Is each service up?* table is all **UP**.
2. **Autopilot: one click, fully autonomous.** Click **Start autopilot**, then click it again within 5 s (it turns yellow: "Click again to start"). The AI planner has
   already chosen 4 experiments from the dependency map (shown as *Planned*, each with its reason):
   1. Payment service crashed  2. Slow network: payment → gateway (+2 s)  3. Order queue (RabbitMQ) stopped
   4. Catalogue service crashed.
   It runs them one by one: waits for a healthy system, injects, measures, lets detection / the kill switch roll it
   back, verifies recovery, moves on. About 7-10 minutes in total; each step turns into **✅ Done** with
   "detected in … · recovered … · proof: …". At the end it writes the findings report by itself.
   Watch the **system map** next to the steps: the tested service is outlined blue while the baseline is measured,
   turns **red with 💥** when the fault lands, everything that depends on it turns **orange (at risk)**, then
   **amber (recovering)** after the rollback and **green (recovered ✓)** at the end. The *What is happening* feed
   narrates each event with a clock time, pop-ups appear bottom-left, and the small chart shows the live error rate
   against the 5 % rollback line. Hover any service or step to see its blast radius.
3. **Show a service is really down** while a crash step is running (steps 1 and 4):
   - the banner turns red ("Fault injected: Catalogue service crashed"), the service's row says **DOWN**, container
     `exited`, health check `timeout`;
   - switch to the **Shop** tab and refresh: with catalogue down the **Categories list is empty**; with payment down,
     **Pay Now** hangs. After the automatic rollback the shop works again, and *Last outage* keeps the proof.
4. **Emergency stop** (red, one click) stops the campaign at any moment: kill switch + rollback of every active
   fault. The kill switch re-arms by itself about 15 s later.
5. **Experiments tab:** read the "Did things get better or worse?" box: ✅ the fix worked (−98.9 %, proven), ⚠️ the
   side effect, ❌ removing the fix brought errors back (proven). Statistics are under *Show the statistics*.
6. **Business impact tab:** the headline gives the costliest failure per incident and per year.
7. **AI report tab:** the report the autopilot just wrote (Planner → Analyzer → Recommender), with
   **Download FINDINGS_REPORT.md**. **Generate report** re-writes it on demand.
8. **Copilot** (💬 button, bottom-right; Esc closes): *"Which service is most fragile?"*, *"Did the fix improve resilience?"*, *"What should we fix first?"*.

Manual mode (pick one experiment yourself) is still there under **Advanced** on the Live tab.

## 4b. Rehearsal checklist (tick each before showing the mentor)

| # | Do | Expect |
| --- | --- | --- |
| 1 | `docker ps` in cmd | 15 `robotshop-*` containers + `chaos-controller-api-1`, all Up / healthy |
| 2 | http://127.0.0.1:8000/api/v1/safety/status | `"is_safe_to_inject": true` |
| 3 | Dashboard http://127.0.0.1:8050, press **Ctrl+F5** | green banner, 4 steps *Planned*, map all green with moving dots, 4 green source dots top-right |
| 4 | Shop: open a robot, then press **F5** on that page | the same robot page reloads (no 404, no crash); also try F5 on Cart and Login |
| 5 | Shop: log in `stan` / `bigbrain`, add a robot, checkout, **Pay Now** | order ID shown |
| 6 | Dashboard: hover *Payment service crashed* | payment red, web orange on the map, the rest dimmed |
| 7 | **Start autopilot** (click twice) | toast "Autopilot started"; step 1 glows with a progress bar; feed starts |
| 8 | During step 1 (payment crash) | map: payment red 💥, web orange; banner red with a ticking outage timer; *Is each service up?* says payment **DOWN**; toasts "Payment is DOWN" then "recovered" |
| 9 | During step 4 (catalogue crash) refresh the Shop | Categories list empty while it is down, back after rollback |
| 10 | After ~8-10 min | all 4 steps ✅ Done with detect/recover times; note says the report is ready |
| 11 | AI report tab | 3 agents in mode *AI* (Gemini), ranked weaknesses, fixes, **Download FINDINGS_REPORT.md** works |
| 12 | Experiments tab | "Did things get better or worse?" findings; charts populated |
| 13 | Business impact tab | costliest-failure banner, brief written by Gemini, cost chart |
| 14 | 💬 Copilot: ask *"Which service is most fragile?"* | grounded answer naming the service and its score |
| 15 | **Emergency stop** while nothing runs | toast "kill switch tripped, no active fault"; safety status back to safe within ~15 s |

If any row fails, see *If something goes wrong* below.

## 4c. Data quality (customer-entered data)

Dashboard → **Data quality** tab:

1. **1 · Simulate messy customer input**: registers demo customers (`dq-…`) and orders through the shop's real
   register and order endpoints: a duplicate customer, blank and null emails, stray spaces and capitals, an email
   typo, an order submitted twice, wrong prices and totals, a missing quantity. The shop accepts all of it.
2. **2 · Scan**: the banner shows the problems found (16 in 15 records, quality score 40 %); the table lists each
   one with type, record, field, before value, proposed after value and rule.
3. **3 · Fix** (click twice): safe fixes are applied (backup first), then it re-scans: quality score 40 % → 69.2 %,
   0 auto-fixable problems left, 4 flagged *needs review* (missing or unrepairable email, unknown quantity: no
   safe guess). The chart and the table show before vs after.
4. **Reset demo data** removes the `dq-…` records. The seeded accounts (`user`, `stan`, `partner-57`) are never changed.

## 5. Same thing from cmd (optional)

The autonomous campaign without the browser (Ctrl+C = emergency stop):

```
cd /d D:\genesis
python scripts\autopilot.py --plan
python scripts\autopilot.py
```

Single experiments:

```
cd /d D:\genesis
python scripts\day1_demo.py --target catalogue
python scripts\day1_demo.py --fault container_crash --target payment
python scripts\day1_demo.py --fault rabbitmq_block --target rabbitmq
python scripts\day1_demo.py --fault network_latency --target payment_to_gateway --params "{\"latency_ms\": 2000, \"jitter_ms\": 200}"
```

Show a fix (stretch goal), then put it back:

```
python scripts\apply_fix.py replicas --service catalogue --count 2
python scripts\day1_demo.py --target catalogue --label "fix: catalogue x2 replicas"
python scripts\apply_fix.py revert --service catalogue
```

Findings report without the dashboard: `cd controller` then `python -m agents report`.

### Saving the Gemini quota for the demo

Each report = 3 calls (repeated identical requests are answered from cache for free), each Copilot question = 1
call, each Business brief = 1 call the first time a run is shown (then cached). Re-opening tabs costs nothing. Generate the
report once after the final autopilot run and leave it.

## 6. Reading the verdicts

- **significant improvement / regression**: the change is larger than the noise of repeating the same experiment
  (two-proportion z-test on failed requests, p < 0.05). We claim it.
- **not significant (noise)**: the difference could be chance, usually because a single run has too few failures to
  tell (e.g. 0 vs 2 failed requests). We do not claim it. Not a bug: it is what stops us claiming fake improvements.
- Highlighted rows compare **whole phases** (all runs before vs after a change, pooled), which is where real changes
  show up; the plain rows repeat the same setup.

## 7. Stop

```
docker compose --env-file .env -f controller/docker-compose.yml down
docker compose -f sandbox/docker-compose.yaml -f sandbox/docker-compose.override.yml down
```

Close cmd window 2 (Ctrl+C) to stop the dashboard. `down` keeps data; adding `-v` deletes it.

## If something goes wrong

| Symptom | Fix |
| --- | --- |
| Dashboard source dot red for *controller* | cmd window 1: re-run the controller `up` command |
| Kill switch says TRIPPED / not safe | wait ~30 s for recovery; Emergency stop if a fault is still active |
| Shop shows 502 after an experiment | `docker restart robotshop-web-1` |
| Shop page shows 404 / tab crashes after a refresh | fixed in `sandbox/web/default.conf.template` (SPA fallback + fixed `<base href="/">`); if it reappears, recreate web: `docker compose -f sandbox/docker-compose.yaml -f sandbox/docker-compose.override.yml up -d --no-deps web` |
| A toxic is stuck (latency persists) | `python -c "import json,urllib.request as u;ps=json.load(u.urlopen('http://127.0.0.1:8474/proxies'));[u.urlopen(u.Request(f'http://127.0.0.1:8474/proxies/{n}/toxics/{t[\"name\"]}',method='DELETE')) for n,p in ps.items() for t in p['toxics']];print('cleared')"` |
| Copilot says "offline" | key missing in root `.env`; no restart needed after adding it |
| AI says "daily AI budget used" or "paused after a quota error" | intentional key protection: answers are cached, at most 200 Gemini calls/day (`LLM_DAILY_BUDGET=` in root `.env` changes it), 15 min pause after a quota error; rule-based answers meanwhile. Usage is shown under the Copilot title |
