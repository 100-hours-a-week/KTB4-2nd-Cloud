#!/usr/bin/env bash

set -Eeuo pipefail

readonly AGENT_CTL="/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl"
readonly AGENT_SERVICE="amazon-cloudwatch-agent"
readonly CONFIG_INSTALL_DIR="/etc/yeodam/cloudwatch-agent"
readonly CONFIG_INSTALL_PATH="${CONFIG_INSTALL_DIR}/app.json"
readonly BACKUP_DIR="/var/backups/amazon-cloudwatch-agent"

config_path=""
backup_path=""
config_existed="false"
agent_was_active="false"
agent_was_enabled="false"
configuration_installed="false"

usage() {
  cat <<'EOF'
Usage:
  sudo ./configure-cloudwatch-agent.sh \
    --config PATH

Options:
  --config
      CloudWatch Agent JSON 설정 파일 경로
EOF
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  echo
  echo "==> $*"
}

rollback() {
  local exit_code=$?

  trap - ERR

  if [[ "${configuration_installed}" == "true" ]]; then
    echo
    echo "CloudWatch Agent 설정 적용에 실패하여 이전 상태를 복구합니다." >&2

    if [[ -n "${backup_path}" && -f "${backup_path}" ]]; then
      install \
        --owner=root \
        --group=root \
        --mode=0644 \
        "${backup_path}" \
        "${CONFIG_INSTALL_PATH}" || true
    elif [[ "${config_existed}" == "false" ]]; then
      rm -f "${CONFIG_INSTALL_PATH}"
    fi

    if [[ "${config_existed}" == "true" &&
      "${agent_was_active}" == "true" &&
      -f "${CONFIG_INSTALL_PATH}" ]]; then
      "${AGENT_CTL}" \
        -a fetch-config \
        -m ec2 \
        -s \
        -c "file:${CONFIG_INSTALL_PATH}" || true
    else
      systemctl stop "${AGENT_SERVICE}" || true
    fi

    if [[ "${agent_was_enabled}" == "true" ]]; then
      systemctl enable "${AGENT_SERVICE}" || true
    else
      systemctl disable "${AGENT_SERVICE}" || true
    fi
  fi

  exit "${exit_code}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      [[ $# -ge 2 ]] || fail "--config 값이 필요합니다."
      config_path="$2"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "지원하지 않는 인자입니다: $1"
      ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || fail "root 권한으로 실행해야 합니다."
[[ -n "${config_path}" ]] || fail "--config을 지정해야 합니다."
[[ -f "${config_path}" ]] || fail "설정 파일을 찾을 수 없습니다: ${config_path}"
[[ -s "${config_path}" ]] || fail "설정 파일이 비어 있습니다: ${config_path}"
[[ -x "${AGENT_CTL}" ]] || fail "CloudWatch Agent가 설치되어 있지 않습니다."

command -v python3 >/dev/null 2>&1 ||
  fail "python3 명령을 찾을 수 없습니다."

command -v systemctl >/dev/null 2>&1 ||
  fail "systemctl 명령을 찾을 수 없습니다."

command -v findmnt >/dev/null 2>&1 ||
  fail "findmnt 명령을 찾을 수 없습니다."

command -v readlink >/dev/null 2>&1 ||
  fail "readlink 명령을 찾을 수 없습니다."

command -v sha256sum >/dev/null 2>&1 ||
  fail "sha256sum 명령을 찾을 수 없습니다."

source /etc/os-release

[[ "${ID}" == "ubuntu" ]] ||
  fail "Ubuntu에서만 실행할 수 있습니다."

[[ "${VERSION_ID}" == "24.04" ]] ||
  fail "Ubuntu 24.04에서만 실행할 수 있습니다."

[[ "$(dpkg --print-architecture)" == "amd64" ]] ||
  fail "amd64 Architecture에서만 실행할 수 있습니다."

config_path="$(readlink -f "${config_path}")"

log "CloudWatch Agent 설정 사전 확인"

python3 -m json.tool "${config_path}" >/dev/null

findmnt --mountpoint / >/dev/null ||
  fail "Root Filesystem Mount를 확인할 수 없습니다."

findmnt --mountpoint /var/lib/mysql >/dev/null ||
  fail "/var/lib/mysql Mount를 확인할 수 없습니다."

if systemctl is-active --quiet "${AGENT_SERVICE}"; then
  agent_was_active="true"
fi

if systemctl is-enabled --quiet "${AGENT_SERVICE}"; then
  agent_was_enabled="true"
fi

install \
  --directory \
  --owner=root \
  --group=root \
  --mode=0755 \
  "${CONFIG_INSTALL_DIR}" \
  "${BACKUP_DIR}"

if [[ -f "${CONFIG_INSTALL_PATH}" ]]; then
  config_existed="true"

  if ! cmp --silent "${config_path}" "${CONFIG_INSTALL_PATH}"; then
    backup_path="$(
      printf '%s/%s.%s' \
        "${BACKUP_DIR}" \
        "app.json" \
        "$(date -u +%Y%m%dT%H%M%SZ)"
    )"

    cp \
      --preserve=mode,timestamps \
      "${CONFIG_INSTALL_PATH}" \
      "${backup_path}"

    log "기존 설정 Backup 생성: ${backup_path}"
  fi
fi

trap rollback ERR

log "CloudWatch Agent 설정 설치"

install \
  --owner=root \
  --group=root \
  --mode=0644 \
  "${config_path}" \
  "${CONFIG_INSTALL_PATH}"

configuration_installed="true"

log "CloudWatch Agent 설정 검증 및 시작"

"${AGENT_CTL}" \
  -a fetch-config \
  -m ec2 \
  -s \
  -c "file:${CONFIG_INSTALL_PATH}"

systemctl enable "${AGENT_SERVICE}"

systemctl is-enabled --quiet "${AGENT_SERVICE}"
systemctl is-active --quiet "${AGENT_SERVICE}"

log "CloudWatch Agent 상태"

"${AGENT_CTL}" -a status

log "적용된 설정 SHA256"

sha256sum "${CONFIG_INSTALL_PATH}"

trap - ERR

echo
echo "CloudWatch Agent Host Metric 구성 완료"