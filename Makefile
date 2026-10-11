# AI chaos-engineering platform - top-level orchestration (M1).
# Windows: run from Git Bash or a WSL shell (needs `make`; see docs/M1_PLATFORM.md -> "Running make on Windows").
# Every target is a plain docker compose command, so the same lines also work typed by hand in cmd/PowerShell.

SB     := docker compose -f sandbox/docker-compose.yaml -f sandbox/docker-compose.override.yml
ENV_FILE ?= .env
CTL    := docker compose --env-file $(ENV_FILE) -f controller/docker-compose.yml
BK_DIR := sandbox-backup/martian-bank-demo
BK     := docker compose --project-directory $(BK_DIR) -f $(BK_DIR)/docker-compose.yaml -f $(BK_DIR)/docker-compose.override.yml
PYTHON ?= python3
API    ?= http://127.0.0.1:8000

.PHONY: help pull up down nuke sandbox-up controller-up ps logs stats verify test-caps test-fill \
        toxics-clear db-export backup-fetch backup-build backup-up backup-down map baseline inject stop report \
        test watchdog-up controller-dev watchdog-dev demo \
        insights dashboard findings fix-replicas fix-revert

help:
	@echo "M1 platform targets:"
	@echo "  make pull          pull all Robot Shop images (do this first, ~2-3 GB)"
	@echo "  make up            start sandbox (waits until healthy) then controller"
	@echo "  make down          stop controller + sandbox (keeps MySQL volume)"
	@echo "  make nuke          down + delete volumes"
	@echo "  make ps | stats | logs svc=cart"
	@echo "  make verify        smoke test: services, proxies, queue, load, RAM"
	@echo "  make test-caps     tmpfs caps are real (safe)   | make test-fill  prove caps hold (fills disks)"
	@echo "  make toxics-clear  remove every Toxiproxy toxic (manual panic for network faults)"
	@echo "  make backup-fetch | backup-build | backup-up | backup-down   Martian Bank hot standby"
	@echo "Team targets: make map (M2) | baseline [dur=600] (M4) | watchdog-up (M4) | inject (M3) | stop (M3/M4) | report (M5) | test | demo"
	@echo "Insights:     make dashboard (http://127.0.0.1:8050: live map, experiments, business impact, AI report, Copilot) | make insights"
	@echo "AI report:    make findings (Planner -> Analyzer -> Recommender agents -> reports/FINDINGS_REPORT.md; llm=0 for rules only)"
	@echo "Fix re-run:   make fix-replicas svc=catalogue n=2 | make fix-revert svc=catalogue"

# ------------------------------------------------------------------ sandbox
pull:
	$(SB) pull

sandbox-up:
	$(SB) up -d --no-build toxiproxy payment-gateway mongodb mysql redis rabbitmq
	$(SB) up -d --no-build --wait --wait-timeout 420

controller-up:
	$(CTL) up -d --build

up: sandbox-up controller-up
	@echo "Shop http://127.0.0.1:8080 | Locust http://127.0.0.1:8089 | RabbitMQ http://127.0.0.1:15672 | Toxiproxy http://127.0.0.1:8474 | Controller $(API)/docs"

down:
	-$(CTL) down
	$(SB) down

nuke:
	-$(CTL) down -v
	$(SB) down -v

ps:
	$(SB) ps
	-$(CTL) ps

logs:
	$(SB) logs --tail 100 $(svc)

stats:
	docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}"

verify:
	$(PYTHON) sandbox/verify.py

test-caps:
	bash sandbox/tests/test_tmpfs_caps.sh

test-fill:
	bash sandbox/tests/test_tmpfs_caps.sh --fill

toxics-clear:
	$(PYTHON) -c "import json,urllib.request as u;ps=json.load(u.urlopen('http://127.0.0.1:8474/proxies'));[u.urlopen(u.Request(f'http://127.0.0.1:8474/proxies/{n}/toxics/{t[\"name\"]}',method='DELETE')) for n,p in ps.items() for t in p['toxics']];print('toxics cleared')"

# ------------------------------------------------------------------ Martian Bank hot standby
backup-fetch:
	bash sandbox-backup/fetch.sh

backup-build: backup-fetch
	$(BK) build

backup-up:
	$(SB) down
	$(BK) up -d
	@echo "Martian Bank on http://127.0.0.1:8080 (UI dev server takes ~1 min)"

backup-down:
	$(BK) down

# ------------------------------------------------------------------ team-owned targets
map:            ## M2
	curl --fail-with-body --silent --show-error -X POST $(API)/api/v1/map

baseline:       ## M4: steady-state capture inside the controller container (writes controller/baseline.json)
	$(CTL) exec api python baseline.py --duration $(or $(dur),600) --interval 5

watchdog-up:    ## M4: dead-man watchdog. Off by default until main.py writes heartbeats (see docs/M1_PLATFORM.md)
	$(CTL) --profile watchdog up -d watchdog

inject:         ## M3: usage make inject fault=container_crash target=cart
	curl --fail-with-body --silent --show-error -X POST "$(API)/api/v1/inject" -H "Content-Type: application/json" -d '{"fault_type": "$(fault)", "target": "$(target)", "ttl_seconds": $(or $(ttl),60)}'

stop:           ## M3/M4: kill switch + rollback
	curl --fail-with-body --silent --show-error -X POST "$(API)/api/v1/safety/trip?reason=ManualStop"

report:         ## M5
	$(CTL) exec api python cli.py report

test:           ## unit tests (install controller/requirements-dev.txt first)
	cd controller && $(PYTHON) -m pytest -q tests
	$(PYTHON) -m pytest -q tests

controller-dev: ## run the API on the host instead of Docker (debugging only)
	cd controller && $(PYTHON) -m uvicorn main:app --host 127.0.0.1 --port 8000

watchdog-dev:   ## run the watchdog on the host (debugging only)
	cd controller && SANDBOX_PROJECT=robotshop $(PYTHON) watchdog.py

demo:
	bash demo_day1.sh

db-export:      ## copy the controller SQLite out of its volume to controller/chaos_state.export.db
	$(CTL) cp api:/data/chaos_state.db controller/chaos_state.export.db

# ------------------------------------------------------------------ insights + agents (host side)
insights:       ## run-to-run improvement, significance, business impact in the terminal
	cd controller && $(PYTHON) -m insights

dashboard:      ## dashboard + Resilience Copilot on http://127.0.0.1:8050 (the controller's /dashboard redirects here)
	cd controller && $(PYTHON) -m insights.dashboard.server

findings:       ## multi-agent findings report (same as the dashboard's Generate report button)
	cd controller && $(PYTHON) -m agents report $(if $(filter 0,$(llm)),--no-llm,)

fix-replicas:   ## stretch goal "re-run after a fix": make fix-replicas svc=catalogue n=2, then re-run the experiment
	$(PYTHON) scripts/apply_fix.py replicas --service $(or $(svc),catalogue) --count $(or $(n),2)

fix-revert:     ## back to 1 replica: make fix-revert svc=catalogue
	$(PYTHON) scripts/apply_fix.py revert --service $(or $(svc),catalogue)
