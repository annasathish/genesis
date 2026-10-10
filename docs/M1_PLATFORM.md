# M1 platform: interface contract

Everything M2-M5 need to integrate with the sandbox. If you need something changed here, ask M1; don't edit `sandbox/` directly.

## Quick start

```bash
powershell -ExecutionPolicy Bypass -File scripts/setup-wslconfig.ps1   # once per laptop, then restart Docker Desktop
make pull          # ~2-3 GB of images, do it early
make up            # sandbox (waits for healthy, ~3 min first time) + controller
make verify        # smoke test
make test-caps     # tmpfs caps are real
```

| URL (localhost only) | What |
| --- | --- |
| http://127.0.0.1:8080 | Robot Shop UI (`web`) |
| http://127.0.0.1:8089 | Locust UI; `GET /stats/requests`, `POST /swarm`, `GET /stop` |
| http://127.0.0.1:15672 | RabbitMQ management (guest/guest); `GET /api/queues` |
| http://127.0.0.1:8474 | Toxiproxy API; `GET /proxies` |
| http://127.0.0.1:8000/docs | Controller (FastAPI) |
| 127.0.0.1:27018 / 3307 / 6380 | Mongo / MySQL / Redis, for probes from the host |

## Layout

```
sandbox/                      compose project "robotshop", network robotshop_robot-shop
  docker-compose.yaml         UNMODIFIED upstream (M2 mapper reads this path)
  docker-compose.override.yml all chaos changes (merged automatically)
  toxiproxy/toxiproxy.json    edge proxies
  locust/locustfile.py        steady load (service `load`)
  verify.py, tests/           smoke test, tmpfs cap test
sandbox-backup/               Martian Bank hot standby (make backup-fetch / backup-build / backup-up)
controller/                   compose project "chaos-controller": services api + watchdog
Makefile, TOOLS_AND_LIBS.md, scripts/setup-wslconfig.ps1
```

## Services (15 containers)

| Service | Tier label | Notes |
| --- | --- | --- |
| web | gateway | nginx + UI, proxies `/api/<svc>/` to each service |
| catalogue, user, cart, shipping, ratings, payment | service | shipping is Java (heap capped 256m) |
| dispatch | worker | consumes `orders` queue from RabbitMQ |
| mongodb, mysql | database | |
| redis | cache | |
| rabbitmq | queue | |
| payment-gateway | external | nginx stub replacing https://paypal.com |
| toxiproxy, load | tooling (`role=infra`) | **never inject into these** |

Labels on every container: `com.chaospilot.role` = `target` | `infra`, `com.chaospilot.tier` as above. Filter with the Docker SDK: `client.containers.list(filters={"label": "com.chaospilot.role=target"})`.

## Toxiproxy edges (M3, M2)

Proxy name = `<caller>_to_<dependency>`. With no toxic attached a proxy is transparent.

| Proxy | Caller env var | Listens | Upstream |
| --- | --- | --- | --- |
| catalogue_to_mongodb | catalogue `MONGO_URL` | 27017 | mongodb:27017 |
| user_to_mongodb | user `MONGO_URL` | 27117 | mongodb:27017 |
| cart_to_redis | cart `REDIS_HOST` | 6379 | redis:6379 |
| cart_to_catalogue | cart `CATALOGUE_HOST` | 8080 | catalogue:8080 |
| shipping_to_mysql | shipping `DB_HOST` | 3306 | mysql:3306 |
| ratings_to_mysql | ratings `PDO_URL` | 13306 | mysql:3306 |
| shipping_to_cart | shipping `CART_ENDPOINT` | 18080 | cart:8080 |
| payment_to_rabbitmq | payment `AMQP_HOST` | 5672 | rabbitmq:5672 |
| payment_to_gateway | payment `PAYMENT_GATEWAY` | 18081 | payment-gateway:80 |

Not proxied (ports are hard-coded in the app or already taken): user→redis, dispatch→rabbitmq, web→services.

Example (M3): 800 ms latency on payment→rabbitmq
```bash
curl -X POST http://127.0.0.1:8474/proxies/payment_to_rabbitmq/toxics \
  -d '{"name":"latency_down","type":"latency","stream":"downstream","attributes":{"latency":800,"jitter":100}}'
curl -X DELETE http://127.0.0.1:8474/proxies/payment_to_rabbitmq/toxics/latency_down
```
`make toxics-clear` deletes every toxic on every proxy.

## Disk exhaustion mounts (M3)

| Service | tmpfs path | Cap | Fill command | Expected effect |
| --- | --- | --- | --- | --- |
| mongodb | /data/db | 128M | `docker exec robotshop-mongodb-1 dd if=/dev/zero of=/data/db/chaos_fill bs=1M count=140` | Mongo writes fail (user register/orders); catalogue reads keep working |
| redis | /data | 64M | `docker exec robotshop-redis-1 dd if=/dev/zero of=/data/chaos_fill bs=1M count=80` | next snapshot (≤30 s) fails, Redis refuses writes: cart add fails |
| mysql | /chaos (MySQL `tmpdir`) | 32M | `docker exec robotshop-mysql-1 dd if=/dev/zero of=/chaos/chaos_fill bs=1M count=40` | queries needing temp files fail |

Rollback: `rm -f <path>/chaos_fill`. tmpfs pages count against the container's `mem_limit`; caps were sized for that.
Mongo and Redis data are in RAM: a restart of those containers re-seeds Mongo and empties Redis (expected, documents it as a finding).

## Load and telemetry (M4)

- Locust service `load`, 10 users by default (`LOCUST_USERS` in `sandbox/.env`). Stats: `GET http://127.0.0.1:8089/stats/requests` (or `http://load:8089` from the controller).
- Request names are stable per endpoint: `web:index`, `catalogue:products|product|categories`, `ratings:fetch|rate`, `user:uniqueid|login`, `cart:add`, `shipping:codes|cities|calc|confirm`, `payment:pay`. `payment:pay` success = end-to-end checkout SLI. The row named `Aggregated` = totals.
- Traffic spike: `POST http://127.0.0.1:8089/swarm` with form fields `user_count=100&spawn_rate=20`; back to `user_count=10`.
- Async health: RabbitMQ `GET /api/queues` (guest/guest) → `orders` queue `messages`, `consumers`.

## Controller (M2, M4, M5)

- `controller/docker-compose.yml`: `api` (uvicorn `main:app`, port 8000) and `watchdog` (`python watchdog/watchdog.py`, placeholder for M4).
- Code is bind-mounted from `controller/`, so edits apply on `docker compose restart api`.
- Image = M2's `controller/Dockerfile` + `requirements.txt` (M1 doesn't touch them). **M2: please add `alembic`, `docker`, `httpx` (and M5: `jinja2`, `typer`) to requirements.txt** - the image currently lacks them.
- Containers have the Docker socket, the sandbox network (reach `toxiproxy:8474`, `load:8089`, `rabbitmq:15672`, `web:8080` by name) and `../sandbox` at `/sandbox` (read-only).
- **SQLite: use `CHAOS_DB_URL` (= `sqlite:////data/chaos_state.db`, a named volume shared by api + watchdog).** SQLite on a Windows bind mount breaks file locking with two writers. M2: please read the env var instead of the hard-coded `./chaos_state.db`. M5 offline report: `docker compose -f controller/docker-compose.yml run --rm api python cli.py report` uses the same volume without the server running.
- Env available: `SANDBOX_DIR`, `SANDBOX_PROJECT`, `TARGET_LABEL`, `TOXIPROXY_URL`, `LOCUST_URL`, `RABBITMQ_API`, `APP_URL`, `GEMINI_API_KEY` (put the key in `controller/.env`, gitignored).

## Deviations from the M1 task file (and why)

| Task file said | Implemented | Why |
| --- | --- | --- |
| WSL cap 6 GB | 4 GB default (`-MemoryGB 6` to override) | host has ~5.2 GB free; a 6 GB VM can push Windows into paging, which corrupts latency measurements |
| MySQL tmpfs `/var/lib/mysql` 128M | MySQL data on a named volume; 32M tmpfs on its tmpdir `/chaos` | Robot Shop's MySQL image moves datadir to `/data`, so `/var/lib/mysql` is unused; the cities dump is 64.6 MB of SQL (~150+ MB on disk + 96 MB redo logs), which would not fit 128M and would cost ~300 MB RAM |
| Toxiproxy example shipping → rabbitmq on 15672 | payment → rabbitmq on 5672 (+ 8 more edges) | shipping never talks to RabbitMQ (payment publishes, dispatch consumes); pika uses port 5672 from the host name, so the proxy must listen on 5672 |
| Run upstream `load` container | our own Locust as service `load` | upstream rs-load runs headless with no stats API; M4 needs `/stats/requests` |
| "11 services" | 12 upstream + payment-gateway stub + toxiproxy + load = 15 | `web`'s nginx needs all 6 API services to start; upstream payment calls the real paypal.com |

## Running make on Windows

`make` isn't on Windows by default. Either run from WSL (Ubuntu) with Docker Desktop's WSL integration, or install it for Git Bash (`winget install ezwinports.make`) and run from Git Bash. Every target is a plain `docker compose` command, so copying the line from the Makefile into cmd also works.

## Resource budget (mem_limit caps, actual use is lower)

Sandbox caps total ~3.2 GB; expected real use ~1.5-2 GB (measure with `make stats`). Controller caps 416 MB. If `make verify` reports > 2.5 GB or the laptop lags: `make backup-up` (Martian Bank).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `mongodb` exits at start, log mentions `wiredTigerEngineConfigString` or "No space left" | remove the `--wiredTigerEngineConfigString` pair from the override and raise its tmpfs to 256M (mem_limit 576m) |
| `shipping` exits 137 / restarts | OOM: `-Xmx384m`, `mem_limit: 576m` |
| `mysql` unhealthy > 4 min on first start | still importing cities: `make logs svc=mysql` |
| `load` exits immediately | Locust tag without `--autostart`: set `LOCUST_TAG` in `sandbox/.env` to a recent 2.x |
| `'!override' ... unknown tag` | Docker Compose older than 2.24.4: update Docker Desktop |
| Instana warnings in service logs | harmless, there is no Instana agent |
