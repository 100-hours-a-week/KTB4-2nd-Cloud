#!/usr/bin/env bash
set -Eeuo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
name="cutover-ci-${RANDOM}-$$"
image="$name:test"
cleanup() {
  docker rm -f "$name-gate" >/dev/null 2>&1 || true
  docker rm -f "$name-redis" >/dev/null 2>&1 || true
  docker network rm "$name" >/dev/null 2>&1 || true
  docker image rm "$image" >/dev/null 2>&1 || true
}
trap cleanup EXIT
# All HTTP tests are mandatory here; host Python lacking aiohttp cannot skip them.
docker build --quiet -t "$image" -f "$root/deploy/request-cutover/Dockerfile" "$root" >/dev/null
docker network create --internal "$name" >/dev/null
docker run -d --name "$name-redis" --network "$name" --network-alias redis redis:7.4.2-alpine >/dev/null
for _ in {1..20}; do
  if docker exec "$name-redis" redis-cli ping 2>/dev/null | rg -q PONG; then break; fi
  sleep 0.2
done
docker run --rm --network "$name" -e CUTOVER_TEST_REDIS_HOST=redis \
  -v "$root:/work:ro" -w /work --entrypoint python "$image" \
  -m unittest discover -s tests/ci -p 'test_request_cutover*.py'

# Python is PID1: SIGINT must reach asyncio cleanup, not wait for Docker SIGKILL.
docker run -d --name "$name-gate" --network "$name" -v "$root:/work:ro" "$image" \
  --config /work/deploy/request-cutover/config.example.json >/dev/null
ready=false
for _ in {1..30}; do
  if docker exec "$name-gate" python -c 'import socket; s=socket.socket(socket.AF_UNIX); s.connect("/run/yeodam-cutover/control.sock")' 2>/dev/null; then ready=true; break; fi
  sleep 0.2
done
[[ "$ready" == true ]]
docker stop --time 5 "$name-gate" >/dev/null
[[ "$(docker inspect --format '{{.State.ExitCode}}' "$name-gate")" == 0 ]]
echo "request-cutover PID1 graceful stop: passed"
