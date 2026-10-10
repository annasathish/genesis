#!/usr/bin/env bash
# Verifies the disk-exhaustion mounts are tmpfs with the expected size caps,
# i.e. filling them can never touch the host / WSL disk.
#
#   bash sandbox/tests/test_tmpfs_caps.sh          # check mounts + small write test (safe while the app runs)
#   bash sandbox/tests/test_tmpfs_caps.sh --fill   # also fill each mount past its cap and prove the cap holds
#                                                   # (briefly breaks writes on that store; cleaned up after)
set -u
cd "$(dirname "$0")/.." || exit 1

FILL=0
[ "${1:-}" = "--fill" ] && FILL=1

# service  mount-path  expected-size-MB
CHECKS="mongodb /data/db 128
redis /data 64
mysql /chaos 32"

fail=0
pass() { echo "[PASS] $*"; }
bad()  { echo "[FAIL] $*"; fail=1; }

while read -r svc path want_mb; do
  cid=$(docker compose ps -q "$svc" 2>/dev/null)
  if [ -z "$cid" ]; then bad "$svc: container not running"; continue; fi

  echo "--- $svc:$path"
  docker exec "$cid" df -h "$path"

  # POSIX df in KiB: Filesystem 1024-blocks Used Available Capacity Mounted-on
  read -r fs size_k used_k _ <<<"$(docker exec "$cid" df -P -k "$path" | awk 'NR==2 {print $1, $2, $3}')"
  size_mb=$(( size_k / 1024 ))

  [ "$fs" = "tmpfs" ] && pass "$svc: $path is tmpfs" || bad "$svc: $path is '$fs', expected tmpfs (host disk exposed!)"
  [ "$size_mb" -eq "$want_mb" ] && pass "$svc: cap is ${size_mb}M" || bad "$svc: cap is ${size_mb}M, expected ${want_mb}M"

  # small write test: 1 MiB in, used grows, file removed
  if docker exec "$cid" sh -c "dd if=/dev/zero of=$path/.m1_probe bs=1M count=1 2>/dev/null && rm -f $path/.m1_probe"; then
    pass "$svc: writable"
  else
    bad "$svc: write to $path failed"
  fi

  if [ "$FILL" -eq 1 ]; then
    over=$(( want_mb + 16 ))
    out=$(docker exec "$cid" sh -c "dd if=/dev/zero of=$path/chaos_fill bs=1M count=$over 2>&1; df -P -k $path | awk 'NR==2 {print \$2, \$3}'; rm -f $path/chaos_fill")
    echo "$out" | grep -q "No space left" && pass "$svc: dd stopped at cap (No space left on device)" \
                                           || bad "$svc: dd of ${over}M did not hit the cap"
  fi
done <<<"$CHECKS"

[ "$fail" -eq 0 ] && echo "ALL TMPFS CHECKS PASSED" || echo "TMPFS CHECKS FAILED"
exit "$fail"
