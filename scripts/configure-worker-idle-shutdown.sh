#!/usr/bin/env bash

set -Eeuo pipefail

readonly TARGET_SCRIPT="/usr/local/sbin/yeodam-worker-idle-shutdown"
readonly CONFIG_DIR="/etc/yeodam"
readonly CONFIG_PATH="${CONFIG_DIR}/worker-idle-shutdown.env"
readonly SERVICE_PATH="/etc/systemd/system/yeodam-worker-idle-shutdown.service"
readonly TIMER_PATH="/etc/systemd/system/yeodam-worker-idle-shutdown.timer"

idle_seconds="600"
recheck_seconds="10"

usage() {
  cat <<'EOF'
Usage:
  sudo ./configure-worker-idle-shutdown.sh \
    [--idle-seconds 600] \
    [--recheck-seconds 10]

Worker /health가 지정한 시간 동안 유휴 상태이면 EC2를 중지하는
systemd Service와 Timer를 설치합니다.
EOF
}

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

is_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --idle-seconds)
      [[ $# -ge 2 ]] || fail "--idle-seconds 값이 필요합니다."
      idle_seconds="$2"
      shift 2
      ;;
    --recheck-seconds)
      [[ $# -ge 2 ]] || fail "--recheck-seconds 값이 필요합니다."
      recheck_seconds="$2"
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
is_positive_integer "${idle_seconds}" || fail "--idle-seconds는 양의 정수여야 합니다."
is_positive_integer "${recheck_seconds}" || fail "--recheck-seconds는 양의 정수여야 합니다."
((idle_seconds >= 60)) || fail "--idle-seconds는 60 이상이어야 합니다."
((recheck_seconds <= 60)) || fail "--recheck-seconds는 60 이하여야 합니다."

for command_name in cat chmod dirname flock install systemctl; do
  command -v "${command_name}" >/dev/null 2>&1 \
    || fail "필요한 명령을 찾을 수 없습니다: ${command_name}"
done

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source_script="${script_directory}/worker-idle-shutdown.sh"
[[ -f "${source_script}" ]] || fail "유휴 종료 Script를 찾을 수 없습니다: ${source_script}"

install --owner=root --group=root --mode=0755 "${source_script}" "${TARGET_SCRIPT}"
install --owner=root --group=root --mode=0755 -d "${CONFIG_DIR}"

cat > "${CONFIG_PATH}" <<EOF
WORKER_HEALTH_URL=http://127.0.0.1:8000/health
WORKER_IDLE_SHUTDOWN_SECONDS=${idle_seconds}
WORKER_IDLE_RECHECK_SECONDS=${recheck_seconds}
EOF
chmod 0644 "${CONFIG_PATH}"

cat > "${SERVICE_PATH}" <<'EOF'
[Unit]
Description=Stop Yeodam AI Worker EC2 after an idle period
After=docker.service network-online.target
Wants=docker.service network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/yeodam/worker-idle-shutdown.env
ExecStart=/usr/bin/flock --nonblock /run/yeodam-worker-idle-shutdown.lock /usr/local/sbin/yeodam-worker-idle-shutdown
EOF

cat > "${TIMER_PATH}" <<'EOF'
[Unit]
Description=Check Yeodam AI Worker idle state every minute

[Timer]
OnBootSec=2min
OnUnitInactiveSec=1min
AccuracySec=5s
Unit=yeodam-worker-idle-shutdown.service

[Install]
WantedBy=timers.target
EOF

chmod 0644 "${SERVICE_PATH}" "${TIMER_PATH}"
systemctl daemon-reload
systemctl enable --now yeodam-worker-idle-shutdown.timer
systemctl restart yeodam-worker-idle-shutdown.timer

echo "Worker 유휴 자동 종료 구성 완료"
echo "Idle threshold: ${idle_seconds}s"
echo "Recheck delay: ${recheck_seconds}s"
systemctl is-enabled yeodam-worker-idle-shutdown.timer
systemctl is-active yeodam-worker-idle-shutdown.timer
