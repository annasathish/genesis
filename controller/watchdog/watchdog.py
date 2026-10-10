"""PLACEHOLDER owned by M4 - replace with the real dead-man watchdog.

M1 only provides the container (see controller/docker-compose.yml, service `watchdog`):
  - same image + code mount as the api, so it can read the SQLite heartbeat/intents
  - Docker socket mounted, so it can `docker start` sandbox containers
  - on the sandbox network: Toxiproxy at TOXIPROXY_URL (DELETE toxics to roll back)
"""
import os
import time

print(f"watchdog placeholder up; toxiproxy={os.getenv('TOXIPROXY_URL')} - M4 to implement", flush=True)
while True:
    time.sleep(60)
