#!/bin/bash
set -Eeuo pipefail
umask 077
mkdir -p /data/db/files
cat /credentials/tls.crt /credentials/tls.key > /tmp/server.pem
if [[ ! -f /data/db/.initialized ]]; then
  [[ ! -f /data/db/.initializing && -z $(ls -A /data/db/files) ]] || { echo 'Existing or partial data requires review; no reset' >&2; exit 1; }
  touch /data/db/.initializing
  # First account creation is TLS-protected and loopback only, never an exposed unauthenticated server.
  mongod --config /runtime/mongod.yaml --bind_ip 127.0.0.1 --noauth &
  init_pid=$!
  trap 'kill -TERM "$init_pid" 2>/dev/null || true; wait "$init_pid" 2>/dev/null || true' EXIT
  for ((i=0;i<90;i++)); do
    if bash /runtime/shell.sh /runtime/initial-ping.js; then break; fi
    kill -0 "$init_pid" 2>/dev/null || exit 1
    sleep 2
  done
  bash /runtime/shell.sh /runtime/init.js
  [[ -f /tmp/accounts-created ]] || { echo 'Account initialization failed; no automatic reset' >&2; exit 1; }
  kill -TERM "$init_pid"
  wait "$init_pid"
  touch /data/db/.initialized
  trap - EXIT
fi
exec mongod --config /runtime/mongod.yaml
