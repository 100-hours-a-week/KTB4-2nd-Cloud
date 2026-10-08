#!/usr/bin/env bash
set -Eeuo pipefail

source deploy/image-versions.env
export FRONTEND_IMAGE FRONTEND_DIGEST BACKEND_IMAGE BACKEND_DIGEST AI_IMAGE AI_DIGEST

compose=(docker compose -f tests/ci/compose.yml)
cleanup() {
  result=$?
  if ((result != 0)); then
    "${compose[@]}" ps || true
    "${compose[@]}" logs --no-color --tail=80 backend ai-worker mysql redis s3 ec2-stub || true
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
export CI_AUTH_SID=00000000-0000-4000-8000-000000000001
session_prefix="$("${compose[@]}" exec -T backend printenv AUTH_SESSION_KEY_PREFIX)"
session_key="${session_prefix}session:${CI_AUTH_SID}"
session_expires_at_ms="$((($(date +%s) + 86400) * 1000))"
"${compose[@]}" exec -T redis redis-cli HSET "${session_key}" \
  userId 900001 \
  refreshTokenHash aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  expiresAt "${session_expires_at_ms}" >/dev/null
"${compose[@]}" exec -T redis redis-cli EXPIRE "${session_key}" 86400 >/dev/null
python3 tests/ci/smoke.py
