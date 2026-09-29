#!/usr/bin/env bash

set -Eeuo pipefail

readonly REGION="${AWS_REGION:-ap-northeast-2}"
readonly APP_INSTANCE_ID="${APP_INSTANCE_ID:-i-051aaa0e3462d2da7}"
readonly WORKER_INSTANCE_ID="${WORKER_INSTANCE_ID:-i-07ae0819b3a21e344}"
readonly REPOSITORY="100-hours-a-week/KTB4-2nd-Cloud"

cloud_commit="${1:-}"
worker_original_state=""
worker_started="false"
worker_idle_shutdown_paused="false"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

ssm_run() {
  local instance_id="$1"
  local comment="$2"
  local command="$3"
  local parameters_file command_id status output error

  parameters_file="$(mktemp)"
  jq -n \
    --arg command "${command}" \
    '{commands: ["exec bash -c " + ($command | @sh)]}' > "${parameters_file}"

  if ! command_id="$(
    aws ssm send-command \
      --region "${REGION}" \
      --instance-ids "${instance_id}" \
      --document-name AWS-RunShellScript \
      --comment "${comment}" \
      --parameters "file://${parameters_file}" \
      --query 'Command.CommandId' \
      --output text
  )"; then
    rm -f -- "${parameters_file}"
    return 1
  fi
  rm -f -- "${parameters_file}"

  local deadline=$((SECONDS + 900))
  while ((SECONDS < deadline)); do
    status="$(aws ssm get-command-invocation --region "${REGION}" --command-id "${command_id}" --instance-id "${instance_id}" --query Status --output text 2>/dev/null || true)"
    case "${status}" in
      Success|Cancelled|Failed|TimedOut|Cancelling)
        break
        ;;
      Pending|InProgress|Delayed)
        sleep 5
        ;;
      "")
        sleep 2
        ;;
      *)
        echo "알 수 없는 SSM 명령 상태입니다: ${status}" >&2
        return 1
        ;;
    esac
  done

  output="$(aws ssm get-command-invocation --region "${REGION}" --command-id "${command_id}" --instance-id "${instance_id}" --query StandardOutputContent --output text)"
  error="$(aws ssm get-command-invocation --region "${REGION}" --command-id "${command_id}" --instance-id "${instance_id}" --query StandardErrorContent --output text)"

  [[ -z "${output}" || "${output}" == "None" ]] || printf '%s\n' "${output}"
  [[ -z "${error}" || "${error}" == "None" ]] || printf '%s\n' "${error}" >&2
  if [[ "${status}" != "Success" ]]; then
    echo "ERROR: SSM 명령이 실패했습니다: instance=${instance_id}, command=${command_id}, status=${status}" >&2
    return 1
  fi
}

wait_for_ssm() {
  local instance_id="$1"
  local deadline=$((SECONDS + 300))
  local status=""

  while ((SECONDS < deadline)); do
    status="$(
      aws ssm describe-instance-information \
        --region "${REGION}" \
        --filters "Key=InstanceIds,Values=${instance_id}" \
        --query 'InstanceInformationList[0].PingStatus' \
        --output text
    )"
    [[ "${status}" == "Online" ]] && return 0
    sleep 5
  done

  fail "SSM 연결을 기다리는 시간이 초과됐습니다: ${instance_id}"
}

worker_state() {
  aws ec2 describe-instances \
    --region "${REGION}" \
    --instance-ids "${WORKER_INSTANCE_ID}" \
    --query 'Reservations[0].Instances[0].State.Name' \
    --output text
}

ensure_worker_ready() {
  local state

  state="$(worker_state)"
  case "${state}" in
    running|pending)
      ;;
    stopping)
      echo "Worker EC2가 종료 중이므로 중지 완료 후 다시 시작합니다."
      aws ec2 wait instance-stopped --region "${REGION}" --instance-ids "${WORKER_INSTANCE_ID}"
      aws ec2 start-instances --region "${REGION}" --instance-ids "${WORKER_INSTANCE_ID}" >/dev/null
      ;;
    stopped)
      echo "Worker EC2를 배포를 위해 시작합니다."
      aws ec2 start-instances --region "${REGION}" --instance-ids "${WORKER_INSTANCE_ID}" >/dev/null
      ;;
    *)
      fail "Worker EC2 상태가 배포 가능한 상태가 아닙니다: ${state}"
      ;;
  esac

  aws ec2 wait instance-status-ok --region "${REGION}" --instance-ids "${WORKER_INSTANCE_ID}"
  wait_for_ssm "${WORKER_INSTANCE_ID}"
}

pause_worker_idle_shutdown() {
  local attempt state

  for attempt in 1 2 3; do
    ensure_worker_ready
    if ssm_run \
      "${WORKER_INSTANCE_ID}" \
      "Pause Yeodam worker idle shutdown" \
      "systemctl stop yeodam-worker-idle-shutdown.timer; systemctl stop yeodam-worker-idle-shutdown.service || true; [[ \"\$(systemctl is-active yeodam-worker-idle-shutdown.timer || true)\" == \"inactive\" ]]"; then
      worker_idle_shutdown_paused="true"
      echo "Worker 유휴 자동 종료를 배포 동안 중지했습니다."
      return 0
    fi

    state="$(worker_state)"
    echo "Worker 유휴 자동 종료 중지에 실패했습니다: attempt=${attempt}, state=${state}" >&2
    [[ "${state}" == "stopping" || "${state}" == "stopped" || "${state}" == "pending" ]] || return 1
  done

  fail "Worker 유휴 자동 종료를 중지하지 못했습니다."
}

restore_worker_state() {
  local exit_code=$?

  trap - EXIT
  if [[ "${worker_idle_shutdown_paused}" == "true" && "$(worker_state)" == "running" ]]; then
    echo "Worker 유휴 자동 종료를 다시 시작합니다."
    ssm_run \
      "${WORKER_INSTANCE_ID}" \
      "Resume Yeodam worker idle shutdown" \
      "systemctl start yeodam-worker-idle-shutdown.timer" \
      || echo "ERROR: Worker 유휴 자동 종료를 다시 시작하지 못했습니다." >&2
  fi

  if [[ "${worker_started}" == "true" && "${worker_original_state}" == "stopped" ]]; then
    echo "Worker EC2를 배포 전 상태로 중지합니다."
    aws ec2 stop-instances --region "${REGION}" --instance-ids "${WORKER_INSTANCE_ID}" >/dev/null
    aws ec2 wait instance-stopped --region "${REGION}" --instance-ids "${WORKER_INSTANCE_ID}"
  fi

  exit "${exit_code}"
}

stage_release() {
  local instance_id="$1"
  local command

  command="$(cat <<EOF
set -Eeuo pipefail
release_root=/opt/yeodam/releases
release_dir="\${release_root}/${cloud_commit}"
install -d --owner=root --group=root --mode=755 "\${release_root}"
if [[ ! -d "\${release_dir}" ]]; then
  staging_dir="\$(mktemp -d "\${release_root}/.staging.XXXXXX")"
  trap 'rm -rf -- "\${staging_dir}"' EXIT
  curl --fail --silent --show-error --location \\
    "https://github.com/${REPOSITORY}/archive/${cloud_commit}.tar.gz" | \\
    tar --extract --gzip --directory "\${staging_dir}" --strip-components=1
  mv -- "\${staging_dir}" "\${release_dir}"
  trap - EXIT
fi
test -f "\${release_dir}/deploy/image-versions.env"
test -x "\${release_dir}/scripts/deploy-release.sh"
test -x "\${release_dir}/scripts/rollback-release.sh"
bash "\${release_dir}/scripts/validate-image-versions.sh" "\${release_dir}/deploy/image-versions.env"
EOF
)"

  ssm_run "${instance_id}" "Stage Yeodam release ${cloud_commit:0:12}" "${command}"
}

deploy_scope() {
  local instance_id="$1"
  local scope="$2"

  ssm_run \
    "${instance_id}" \
    "Deploy Yeodam ${scope} ${cloud_commit:0:12}" \
    "bash /opt/yeodam/releases/${cloud_commit}/scripts/deploy-release.sh --scope ${scope} --release-dir /opt/yeodam/releases/${cloud_commit} --region ${REGION}"
}

recover_scope() {
  local instance_id="$1"
  local scope="$2"
  local target="$3"

  echo "${scope}을 ${target} 성공 Release로 복구합니다."
  ssm_run \
    "${instance_id}" \
    "Recover Yeodam ${scope} from ${target}" \
    "bash /opt/yeodam/releases/${cloud_commit}/scripts/rollback-release.sh --scope ${scope} --target ${target}"
}

[[ "${cloud_commit}" =~ ^[0-9a-f]{40}$ ]] || fail "Cloud Commit은 40자리 SHA여야 합니다."
for command_name in aws jq; do
  command -v "${command_name}" >/dev/null 2>&1 || fail "필요한 명령을 찾을 수 없습니다: ${command_name}"
done

app_state="$(aws ec2 describe-instances --region "${REGION}" --instance-ids "${APP_INSTANCE_ID}" --query 'Reservations[0].Instances[0].State.Name' --output text)"
[[ "${app_state}" == "running" ]] || fail "App EC2가 running 상태가 아닙니다: ${app_state}"
wait_for_ssm "${APP_INSTANCE_ID}"

worker_original_state="$(aws ec2 describe-instances --region "${REGION}" --instance-ids "${WORKER_INSTANCE_ID}" --query 'Reservations[0].Instances[0].State.Name' --output text)"
case "${worker_original_state}" in
  running)
    ;;
  stopped)
    worker_started="true"
    ;;
  stopping)
    echo "Worker EC2가 배포 시작 시 종료 중이므로 중지 상태로 복원합니다."
    worker_original_state="stopped"
    worker_started="true"
    ;;
  pending)
    echo "Worker EC2가 배포 시작 시 기동 중이므로 실행 상태로 유지합니다."
    worker_original_state="running"
    ;;
  *)
    fail "Worker EC2 상태가 배포 가능한 상태가 아닙니다: ${worker_original_state}"
    ;;
esac
trap restore_worker_state EXIT
ensure_worker_ready
pause_worker_idle_shutdown

echo "App/Worker에 Release를 준비합니다: ${cloud_commit}"
stage_release "${APP_INSTANCE_ID}"
ensure_worker_ready
stage_release "${WORKER_INSTANCE_ID}"

echo "App Release를 배포합니다."
if ! deploy_scope "${APP_INSTANCE_ID}" app; then
  recover_scope "${APP_INSTANCE_ID}" app current \
    || echo "ERROR: App current Release 재적용도 실패했습니다." >&2
  fail "App 배포가 실패했습니다."
fi

echo "Worker Release를 배포합니다."
if ! deploy_scope "${WORKER_INSTANCE_ID}" worker; then
  recover_scope "${WORKER_INSTANCE_ID}" worker current \
    || echo "ERROR: Worker current Release 재적용도 실패했습니다." >&2
  recover_scope "${APP_INSTANCE_ID}" app previous \
    || echo "ERROR: App previous Release Rollback도 실패했습니다." >&2
  fail "Worker 배포가 실패하여 App/Worker 복구를 시도했습니다."
fi

echo "App/Worker 배포 완료: ${cloud_commit}"
