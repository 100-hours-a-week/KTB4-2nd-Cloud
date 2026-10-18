#!/usr/bin/env bash
# Only operates on a run-specific disposable Compose project, never shared staging.
set -Eeuo pipefail
umask 077
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
runtime="${2:?Usage: request-cutover.sh init|up|seed|before|check|status|cutover|after|verify|oauth|metrics|logs|stop-v1|stop-bridge RUNTIME [arguments]}"
command="${1:?command required}"
shift 2
[[ "$runtime" == /* ]] || { echo 'Runtime must be absolute' >&2; exit 1; }
if [[ "$command" == init ]]; then
  PYTHONPATH="$root/scripts" python3 -m request_cutover.prepare --runtime "$runtime" --root "$root"
  exit
fi
run=$(python3 - "$runtime" <<'PY'
import json, pathlib, sys
from uuid import UUID
p=pathlib.Path(sys.argv[1]); c=json.loads((p/'config.json').read_text())
assert p.stat().st_mode & 0o077 == 0
assert (p/'runtime.env').stat().st_mode & 0o077 == 0
r=c['run_id']; assert str(UUID(r)) == r
assert c['environment'] == 'isolated-staging'
print(r)
PY
)
project="yeodam-cutover-${run:0:8}"
if docker compose version >/dev/null 2>&1; then
  compose=(docker compose)
else
  compose=(docker-compose)
fi
compose+=(--project-name "$project" --env-file "$runtime/runtime.env" -f "$root/deploy/request-cutover/compose.yml")
ctl() { "${compose[@]}" exec -T gate python -m request_cutover.control "$@" --config /private/config.json; }
fixture() { "${compose[@]}" exec -T gate python -m request_cutover.fixture "$@" --config /private/config.json --pool /private/sessions.json; }
case "$command" in
  up)
    "${compose[@]}" config --quiet
    "${compose[@]}" up -d --wait --wait-timeout 180 mysql redis ec2-stub s3
    "${compose[@]}" up -d --wait --wait-timeout 180 v1-backend
    "${compose[@]}" up -d --wait --wait-timeout 180 v2-backend v2-ingress
    "${compose[@]}" --profile browser up -d v1-frontend v2-frontend
    "${compose[@]}" up -d --build gate ingress metrics
    ;;
  seed|before|after|verify|oauth) fixture "$command" "$@" ;;
  check|status|metrics|cutover) ctl "$command" "$@" ;;
  logs) "${compose[@]}" logs --no-color --no-log-prefix gate ;;
  stop-v1|stop-bridge)
    ctl status | python3 -c 'import json,sys; assert json.load(sys.stdin)["phase"] == "V2", "Refuse to stop before commit"'
    ctl check >/dev/null
    fixture verify --session-index 1 --base https://v2-ingress
    if [[ "$command" == stop-v1 ]]; then
      "${compose[@]}" --profile browser stop --timeout 15 v1-backend v1-frontend
    else
      # Verify direct V2 first. A different synthetic SID remains active after the logout smoke.
      # Nginx first: stop accepting old connections and drain responses before gate exits.
      "${compose[@]}" stop --timeout 620 ingress
      "${compose[@]}" stop --timeout 620 gate metrics
      "${compose[@]}" run --rm --no-deps --entrypoint python gate \
        -m request_cutover.fixture verify --session-index 1 --base https://v2-ingress \
        --config /private/config.json --pool /private/sessions.json
    fi
    ;;
  *) echo "Unknown command: $command" >&2; exit 1 ;;
esac
