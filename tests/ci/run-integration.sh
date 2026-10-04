#!/usr/bin/env bash
set -Eeuo pipefail

source deploy/image-versions.env
export FRONTEND_IMAGE FRONTEND_DIGEST BACKEND_IMAGE BACKEND_DIGEST AI_IMAGE AI_DIGEST

compose=(docker compose -f tests/ci/compose.yml)
cleanup() {
  result=$?
  if ((result != 0)); then
    "${compose[@]}" ps || true
    "${compose[@]}" logs --no-color --tail=80 backend ai-worker mysql s3 ec2-stub || true
  fi
  "${compose[@]}" down --volumes --remove-orphans || true
  exit "${result}"
}
trap cleanup EXIT

"${compose[@]}" up --detach --wait --wait-timeout 240
"${compose[@]}" exec -T s3 awslocal s3 mb s3://yeodam-ci >/dev/null

wait_for_http() {
  local label="$1" url="$2" deadline=$((SECONDS + 240))
  until curl --fail --silent "${url}" >/dev/null; do
    ((SECONDS < deadline)) || {
      echo "${label} 기동 대기 시간이 초과됐습니다." >&2
      exit 1
    }
    sleep 5
  done
}
wait_for_http Frontend http://127.0.0.1:13000/api/health
wait_for_http Backend http://127.0.0.1:18080/api/actuator/health
wait_for_http AI http://127.0.0.1:18000/health

"${compose[@]}" exec -T mysql mysql --user=root --password=ci-root-only yeodam \
  < tests/ci/seed.sql
python3 tests/ci/smoke.py
