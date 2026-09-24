#!/usr/bin/env bash

set -Eeuo pipefail

readonly STATE_ROOT="/opt/yeodam/state"

scope=""

usage() {
  cat <<'EOF'
Usage:
  sudo ./rollback-release.sh --scope app|worker

The script deploys the release recorded in /opt/yeodam/state/{scope}/previous.
EOF
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope)
      [[ $# -ge 2 ]] || fail "--scope 값이 필요합니다."
      scope="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      fail "알 수 없는 옵션입니다: $1"
      ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || fail "이 스크립트는 root로 실행해야 합니다."
[[ "${scope}" == "app" || "${scope}" == "worker" ]] || fail "--scope는 app 또는 worker여야 합니다."

previous_file="${STATE_ROOT}/${scope}/previous"
[[ -f "${previous_file}" ]] || fail "이전 성공 Release 기록이 없습니다: ${previous_file}"

previous_release="$(<"${previous_file}")"
[[ "${previous_release}" == "/opt/yeodam/releases/"* ]] || fail "잘못된 이전 Release 경로입니다."
[[ -d "${previous_release}" ]] || fail "이전 Release Directory가 없습니다: ${previous_release}"
[[ -f "${previous_release}/scripts/deploy-release.sh" ]] || fail "이전 Release에 배포 Script가 없습니다."

exec bash "${previous_release}/scripts/deploy-release.sh" \
  --scope "${scope}" \
  --release-dir "${previous_release}" \
  --state-action rollback
