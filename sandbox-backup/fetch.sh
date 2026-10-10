#!/usr/bin/env bash
# Fetch Martian Bank (hot-standby target) at a pinned commit and drop in our override + env files.
# Usage: make backup-fetch   (then `make backup-build` once, while the venue Wi-Fi is quiet)
set -euo pipefail
cd "$(dirname "$0")"

REPO=https://github.com/cisco-open/martian-bank-demo
COMMIT=8986cf0e18734fc8bb0cc81bf139fe4cb92af8ac   # pinned 2026-10-10
DIR=martian-bank-demo

if [ ! -d "$DIR/.git" ]; then
  git clone "$REPO" "$DIR"
fi
git -C "$DIR" fetch --quiet origin "$COMMIT" 2>/dev/null || true
git -C "$DIR" checkout --quiet "$COMMIT"

cp docker-compose.override.yml "$DIR/docker-compose.override.yml"

# env_file entries in upstream compose must exist or compose refuses to start
DB_URL="mongodb://root:example@mongo:27017/"
printf 'DB_URL=%s\nJWT_SECRET=chaospilot-local-only\nNODE_ENV=production\n' "$DB_URL" > "$DIR/customer-auth/.env"
printf 'DB_URL=%s\nNODE_ENV=production\n' "$DB_URL" > "$DIR/atm-locator/.env"
: > "$DIR/ui/.env"

echo "Martian Bank ready in sandbox-backup/$DIR @ ${COMMIT:0:7}. Next: make backup-build"
