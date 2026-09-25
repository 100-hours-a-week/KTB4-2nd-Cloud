#!/usr/bin/env bash

set -Eeuo pipefail

readonly DEFAULT_HEALTH_URL="http://127.0.0.1:8000/health"
readonly DEFAULT_IDLE_SECONDS="600"
readonly DEFAULT_RECHECK_SECONDS="10"

health_url="${WORKER_HEALTH_URL:-${DEFAULT_HEALTH_URL}}"
idle_threshold_seconds="${WORKER_IDLE_SHUTDOWN_SECONDS:-${DEFAULT_IDLE_SECONDS}}"
recheck_seconds="${WORKER_IDLE_RECHECK_SECONDS:-${DEFAULT_RECHECK_SECONDS}}"
dry_run="false"

usage() {
  cat <<'EOF'
Usage:
  sudo ./worker-idle-shutdown.sh [--dry-run]

Environment:
  WORKER_HEALTH_URL
  WORKER_IDLE_SHUTDOWN_SECONDS
  WORKER_IDLE_RECHECK_SECONDS
EOF
}

log() {
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*"
}

is_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

check_idle() {
  local response parsed status active_tasks queued idle_seconds

  if ! response="$({
    curl \
      --fail \
      --silent \
      --show-error \
      --max-time 5 \
      "${health_url}"
  } 2>/dev/null)"; then
    log "Worker Health를 확인할 수 없어 종료를 건너뜁니다."
    return 1
  fi

  if ! parsed="$(python3 -c '
import json
import sys

data = json.loads(sys.argv[1])
status = data.get("status")
active = data.get("active_tasks")
queued = data.get("queued")
idle = data.get("idle_seconds")

if status not in {"ok", "starting", "degraded"}:
    raise ValueError("invalid status")
if any(type(value) is not int or value < 0 for value in (active, queued, idle)):
    raise ValueError("invalid counters")

print(f"{status}\t{active}\t{queued}\t{idle}")
' "${response}" 2>/dev/null)"; then
    log "Worker Health 응답을 해석할 수 없어 종료를 건너뜁니다."
    return 1
  fi

  IFS=$'\t' read -r status active_tasks queued idle_seconds <<<"${parsed}"
  log "Worker 상태: status=${status}, active_tasks=${active_tasks}, queued=${queued}, idle_seconds=${idle_seconds}"

  [[ "${status}" == "ok" ]] || return 1
  ((active_tasks == 0)) || return 1
  ((queued == 0)) || return 1
  ((idle_seconds >= idle_threshold_seconds)) || return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      dry_run="true"
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: 알 수 없는 옵션입니다: $1" >&2
      exit 1
      ;;
  esac
done

[[ "${EUID}" -eq 0 || "${dry_run}" == "true" ]] || {
  echo "ERROR: 실제 종료 실행은 root 권한이 필요합니다." >&2
  exit 1
}

is_positive_integer "${idle_threshold_seconds}" || {
  echo "ERROR: WORKER_IDLE_SHUTDOWN_SECONDS는 양의 정수여야 합니다." >&2
  exit 1
}
is_positive_integer "${recheck_seconds}" || {
  echo "ERROR: WORKER_IDLE_RECHECK_SECONDS는 양의 정수여야 합니다." >&2
  exit 1
}
((idle_threshold_seconds >= 60)) || {
  echo "ERROR: 유휴 종료 기준은 60초 이상이어야 합니다." >&2
  exit 1
}

for command_name in curl date python3 sleep; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "ERROR: 필요한 명령을 찾을 수 없습니다: ${command_name}" >&2
    exit 1
  }
done

if [[ "${dry_run}" != "true" ]]; then
  command -v systemctl >/dev/null 2>&1 || {
    echo "ERROR: 필요한 명령을 찾을 수 없습니다: systemctl" >&2
    exit 1
  }
fi

if ! check_idle; then
  exit 0
fi

log "종료 직전 상태를 ${recheck_seconds}초 후 다시 확인합니다."
sleep "${recheck_seconds}"

if ! check_idle; then
  log "재확인 결과 종료 조건을 만족하지 않아 Worker를 유지합니다."
  exit 0
fi

if [[ "${dry_run}" == "true" ]]; then
  log "Dry Run: Worker EC2 종료 조건을 만족했습니다."
  exit 0
fi

log "Worker가 ${idle_threshold_seconds}초 이상 유휴 상태이므로 EC2를 중지합니다."
systemctl poweroff
