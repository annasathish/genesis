# AI chaos-engineering platform - top-level orchestration (M1).
# Windows: run from Git Bash or a WSL shell (needs `make`; see docs/M1_PLATFORM.md -> "Running make on Windows").
# Every target is a plain docker compose command, so the same lines also work typed by hand in cmd/PowerShell.

SB     := docker compose -f sandbox/docker-compose.yaml -f sandbox/docker-compose.override.yml
CTL    := docker compose -f controller/docker-compose.yml
BK_DIR := sandbox-backup/martian-bank-demo
BK     := docker compose --project-directory $(BK_DIR) -f $(BK_DIR)/docker-compose.yaml -f $(BK_DIR)/docker-compose.override.yml
PYTHON ?= python
API    ?= http://127.0.0.1:8000

.PHONY: help pull up down nuke sandbox-up controller-up ps logs stats verify test-caps test-fill \
        toxics-clear db-export backup-fetch backup-build backup-up backup-down map baseline inject stop report \
        test watchdog-up controller-dev watchdog-dev demo insights dashboard

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
	@echo "Insights:     make insights (run-to-run improvement + business impact) | make dashboard (http://127.0.0.1:8050 + Copilot chat)"

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
	curl -s -X POST $(API)/api/v1/map

baseline:       ## M4: steady-state capture inside the controller container (writes controller/baseline.json)
	$(CTL) exec api python baseline.py --duration $(or $(dur),600) --interval 5

watchdog-up:    ## M4: dead-man watchdog. Off by default until main.py writes heartbeats (see docs/M1_PLATFORM.md)
	$(CTL) --profile watchdog up -d watchdog

inject:         ## M3: usage make inject fault=container_crash target=cart
	@echo "make inject is owned by M3 - add the command here (fault=$(fault) target=$(target))" && exit 1

stop:           ## M3/M4: kill switch + rollback
	@echo "make stop is owned by M3/M4 - until then use: make toxics-clear && make sandbox-up" && exit 1

report:         ## M5
	$(CTL) exec api python cli.py report

test:           ## all unit tests (host Python; pip install -r controller/requirements.txt pytest pytest-asyncio)
	$(PYTHON) -m pytest -q tests controller/test_reporting.py
	cd controller && $(PYTHON) -m pytest -q tests

controller-dev: ## run the API on the host instead of Docker (debugging only)
	cd controller && $(PYTHON) -m uvicorn main:app --host 127.0.0.1 --port 8000

watchdog-dev:   ## run the watchdog on the host (debugging only)
	cd controller && SANDBOX_PROJECT=robotshop $(PYTHON) watchdog.py

demo:
	bash demo_day1.sh

db-export:      ## copy the controller SQLite out of its volume to controller/chaos_state.export.db
	$(CTL) cp api:/data/chaos_state.db controller/chaos_state.export.db

# ------------------------------------------------------------------ insights (read-only over runs/*.json, host side)
insights:       ## run-to-run improvement, significance, business impact in the terminal
	cd controller && $(PYTHON) -m insights

dashboard:      ## interactive dashboard + Resilience Copilot chat on http://127.0.0.1:8050 (GEMINI_API_KEY optional)
	cd controller && $(PYTHON) -m insights.dashboard.server
