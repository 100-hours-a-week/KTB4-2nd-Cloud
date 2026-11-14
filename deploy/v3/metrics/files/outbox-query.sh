#!/bin/bash
set -Eeuo pipefail
umask 077
# Never interpolate credentials or arbitrary SQL. All failures invalidate this entire sample.
while true; do
  started=$(date +%s.%N)
  if timeout 8 mysql --defaults-extra-file=/outbox-auth/client.cnf --batch --skip-column-names --silent < /exporter/outbox.sql > /outbox/rows.new 2>/dev/null; then
    { printf "%s\t%s\n" "$started" "$(date +%s.%N)"; cat /outbox/rows.new; } > /outbox/snapshot.new
    mv /outbox/snapshot.new /outbox/snapshot
  else
    rm -f /outbox/snapshot
  fi
  rm -f /outbox/rows.new
  sleep 5
done
