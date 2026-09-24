#!/usr/bin/env bash

set -Eeuo pipefail

readonly STATE_ROOT="/opt/yeodam/state"

scope=""
target="previous"

usage() {
  cat <<'EOF'
Usage:
  sudo ./rollback-release.sh --scope app|worker [--target previous|current]

The script deploys the release recorded in /opt/yeodam/state/{scope}/{target}.
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
    --target)
      [[ $# -ge 2 ]] || fail "--target 값이 필요합니다."
      target="$2"
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
[[ "${target}" == "previous" || "${target}" == "current" ]] || fail "--target은 previous 또는 current여야 합니다."

target_file="${STATE_ROOT}/${scope}/${target}"
[[ -f "${target_file}" ]] || fail "성공 Release 기록이 없습니다: ${target_file}"

target_release="$(<"${target_file}")"
[[ "${target_release}" == "/opt/yeodam/releases/"* ]] || fail "잘못된 Release 경로입니다."
[[ -d "${target_release}" ]] || fail "Release Directory가 없습니다: ${target_release}"
[[ -f "${target_release}/scripts/deploy-release.sh" ]] || fail "Release에 배포 Script가 없습니다."

state_action="deploy"
[[ "${target}" == "previous" ]] && state_action="rollback"

exec bash "${target_release}/scripts/deploy-release.sh" \
  --scope "${scope}" \
  --release-dir "${target_release}" \
  --state-action "${state_action}"
