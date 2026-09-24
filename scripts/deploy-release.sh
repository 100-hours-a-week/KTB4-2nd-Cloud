#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

readonly RELEASE_ROOT="/opt/yeodam/releases"
readonly STATE_ROOT="/opt/yeodam/state"
readonly DEFAULT_REGION="ap-northeast-2"
readonly DEFAULT_PARAMETER_PREFIX="/yeodam/v1"

scope=""
release_dir=""
aws_region="${AWS_REGION:-${DEFAULT_REGION}}"
parameter_prefix="${DEFAULT_PARAMETER_PREFIX}"
state_action="deploy"
temporary_directory=""
nginx_restore_required="false"
nginx_target="/etc/nginx/sites-available/yeodam"
nginx_enabled="/etc/nginx/sites-enabled/yeodam"
maintenance_enabled="/etc/nginx/sites-enabled/yeodam-maintenance"

usage() {
  cat <<'EOF'
Usage:
  sudo ./deploy-release.sh \
    --scope app|worker \
    --release-dir /opt/yeodam/releases/CLOUD_COMMIT_SHA \
    [--region AWS_REGION] \
    [--parameter-prefix PARAMETER_PATH]

The release directory must contain:
  app:    app-compose.yml, nginx/yeodam.conf, deploy/image-versions.env
  worker: worker-compose.yml, deploy/image-versions.env
  both:   scripts/render-runtime-env.sh
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

cleanup() {
  local exit_code=$?

  trap - EXIT ERR

  if [[ "${nginx_restore_required}" == "true" ]]; then
    restore_nginx || true
  fi

  if [[ -n "${temporary_directory}" && -d "${temporary_directory}" ]]; then
    rm -rf -- "${temporary_directory}"
  fi

  exit "${exit_code}"
}

restore_nginx() {
  local nginx_backup="${temporary_directory}/nginx.conf.previous"

  if [[ -f "${nginx_backup}" ]]; then
    install --owner=root --group=root --mode=0644 "${nginx_backup}" "${nginx_target}"
  else
    rm -f -- "${nginx_target}"
  fi

  if [[ -f "${temporary_directory}/yeodam-enabled" ]]; then
    ln -sfn "${nginx_target}" "${nginx_enabled}"
  else
    rm -f -- "${nginx_enabled}"
  fi

  if [[ -f "${temporary_directory}/maintenance-enabled" ]]; then
    ln -sfn "/etc/nginx/sites-available/yeodam-maintenance" "${maintenance_enabled}"
  else
    rm -f -- "${maintenance_enabled}"
  fi

  nginx -t >/dev/null 2>&1 && systemctl reload nginx
}

read_env_value() {
  local env_file="$1"
  local key="$2"
  local value

  value="$(awk -F= -v key="${key}" '$1 == key { count++; value = substr($0, index($0, "=") + 1) } END { if (count == 1) print value }' "${env_file}")"
  [[ -n "${value}" ]] || fail "${env_file}에서 ${key} 값을 하나만 지정해야 합니다."
  printf '%s' "${value}"
}

validate_image_reference() {
  local image="$1"
  local digest="$2"
  local expected_package="$3"

  [[ "${image}" =~ ^ghcr\.io/100-hours-a-week/${expected_package}:sha-[0-9a-f]{40}$ ]] \
    || fail "허용하지 않은 Image 형식입니다: ${expected_package}"
  [[ "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || fail "잘못된 Image Digest 형식입니다: ${expected_package}"
}

wait_for_health() {
  local timeout_seconds="$1"
  shift
  local deadline=$((SECONDS + timeout_seconds))
  local service container_id health_status

  while ((SECONDS < deadline)); do
    local all_healthy="true"

    for service in "$@"; do
      container_id="$("${compose_command[@]}" ps --quiet "${service}")"
      if [[ -z "${container_id}" ]]; then
        all_healthy="false"
        continue
      fi

      health_status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}")"
      if [[ "${health_status}" != "healthy" ]]; then
        [[ "${health_status}" != "exited" && "${health_status}" != "dead" ]] \
          || fail "${service} Container가 ${health_status} 상태입니다."
        all_healthy="false"
      fi
    done

    [[ "${all_healthy}" == "true" ]] && return 0
    sleep 5
  done

  fail "Container Health 확인 시간이 초과됐습니다."
}

verify_image() {
  local service="$1"
  local image="$2"
  local digest="$3"
  local container_id expected_id actual_id

  container_id="$("${compose_command[@]}" ps --quiet "${service}")"
  expected_id="$(docker image inspect "${image}@${digest}" --format '{{.Id}}')"
  actual_id="$(docker inspect --format '{{.Image}}' "${container_id}")"

  [[ "${actual_id}" == "${expected_id}" ]] \
    || fail "${service}가 지정한 Image Digest로 실행되지 않았습니다."
}

record_release_state() {
  local state_dir="${STATE_ROOT}/${scope}"
  local current_file="${state_dir}/current"
  local previous_file="${state_dir}/previous"
  local current_release=""

  install -d --owner=root --group=root --mode=0700 "${state_dir}"
  if [[ -f "${current_file}" ]]; then
    current_release="$(<"${current_file}")"
  fi

  if [[ "${state_action}" == "rollback" ]]; then
    [[ -n "${current_release}" ]] && printf '%s\n' "${current_release}" > "${previous_file}.tmp"
  elif [[ -n "${current_release}" && "${current_release}" != "${release_dir}" ]]; then
    printf '%s\n' "${current_release}" > "${previous_file}.tmp"
  fi

  printf '%s\n' "${release_dir}" > "${current_file}.tmp"
  install --owner=root --group=root --mode=0600 "${current_file}.tmp" "${current_file}"
  rm -f -- "${current_file}.tmp"

  if [[ -f "${previous_file}.tmp" ]]; then
    install --owner=root --group=root --mode=0600 "${previous_file}.tmp" "${previous_file}"
    rm -f -- "${previous_file}.tmp"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope)
      [[ $# -ge 2 ]] || fail "--scope 값이 필요합니다."
      scope="$2"
      shift 2
      ;;
    --release-dir)
      [[ $# -ge 2 ]] || fail "--release-dir 값이 필요합니다."
      release_dir="$2"
      shift 2
      ;;
    --region)
      [[ $# -ge 2 ]] || fail "--region 값이 필요합니다."
      aws_region="$2"
      shift 2
      ;;
    --parameter-prefix)
      [[ $# -ge 2 ]] || fail "--parameter-prefix 값이 필요합니다."
      parameter_prefix="$2"
      shift 2
      ;;
    --state-action)
      [[ $# -ge 2 ]] || fail "--state-action 값이 필요합니다."
      state_action="$2"
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
[[ "${state_action}" == "deploy" || "${state_action}" == "rollback" ]] || fail "잘못된 상태 기록 방식입니다."
[[ -n "${release_dir}" ]] || fail "--release-dir이 필요합니다."
[[ "${release_dir}" == "${RELEASE_ROOT}/"* ]] || fail "Release는 ${RELEASE_ROOT} 아래에 있어야 합니다."
[[ -d "${release_dir}" ]] || fail "Release Directory를 찾을 수 없습니다: ${release_dir}"
[[ "${parameter_prefix}" == /* ]] || fail "--parameter-prefix는 /로 시작해야 합니다."
parameter_prefix="${parameter_prefix%/}"

for command_name in aws awk curl docker install mktemp python3 realpath; do
  command -v "${command_name}" >/dev/null 2>&1 || fail "필요한 명령을 찾을 수 없습니다: ${command_name}"
done

if [[ "${scope}" == "app" ]]; then
  for command_name in nginx systemctl; do
    command -v "${command_name}" >/dev/null 2>&1 || fail "필요한 명령을 찾을 수 없습니다: ${command_name}"
  done
fi

release_dir="$(realpath -e -- "${release_dir}")"
[[ "${release_dir}" == "${RELEASE_ROOT}/"* ]] || fail "Release 실제 경로가 ${RELEASE_ROOT} 밖을 가리킵니다."
release_name="${release_dir##*/}"
[[ "${release_name}" =~ ^[0-9a-f]{7,40}$ ]] || fail "Release Directory 이름은 Cloud Commit SHA여야 합니다."

readonly runtime_renderer="${release_dir}/scripts/render-runtime-env.sh"
readonly image_env="${release_dir}/deploy/image-versions.env"
[[ -f "${runtime_renderer}" ]] || fail "Runtime env Script를 찾을 수 없습니다: ${runtime_renderer}"
[[ -f "${image_env}" ]] || fail "Image Version 파일을 찾을 수 없습니다: ${image_env}"

if [[ "${scope}" == "app" ]]; then
  readonly compose_file="${release_dir}/app-compose.yml"
  readonly runtime_env="/run/yeodam/app.env"
  readonly nginx_source="${release_dir}/nginx/yeodam.conf"
  [[ -f "${nginx_source}" ]] || fail "Nginx 설정을 찾을 수 없습니다: ${nginx_source}"
  services=(backend frontend)
  health_timeout=120
else
  readonly compose_file="${release_dir}/worker-compose.yml"
  readonly runtime_env="/run/yeodam/worker.env"
  services=(ai-worker)
  health_timeout=180
fi
[[ -f "${compose_file}" ]] || fail "Compose 파일을 찾을 수 없습니다: ${compose_file}"

if [[ "${scope}" == "app" ]]; then
  frontend_image="$(read_env_value "${image_env}" FRONTEND_IMAGE)"
  frontend_digest="$(read_env_value "${image_env}" FRONTEND_DIGEST)"
  backend_image="$(read_env_value "${image_env}" BACKEND_IMAGE)"
  backend_digest="$(read_env_value "${image_env}" BACKEND_DIGEST)"
  validate_image_reference "${frontend_image}" "${frontend_digest}" yeodam-frontend
  validate_image_reference "${backend_image}" "${backend_digest}" yeodam-backend
else
  ai_image="$(read_env_value "${image_env}" AI_IMAGE)"
  ai_digest="$(read_env_value "${image_env}" AI_DIGEST)"
  validate_image_reference "${ai_image}" "${ai_digest}" yeodam-ai-worker
fi

temporary_directory="$(mktemp -d /run/yeodam-deploy.XXXXXX)"
trap cleanup EXIT ERR

log "Runtime 환경변수 생성"
bash "${runtime_renderer}" \
  --scope "${scope}" \
  --output "${runtime_env}" \
  --region "${aws_region}" \
  --path-prefix "${parameter_prefix}"

log "GHCR 임시 인증"
ghcr_username="$(aws ssm get-parameter --region "${aws_region}" --name "${parameter_prefix}/deploy/GHCR_USERNAME" --query 'Parameter.Value' --output text --no-cli-pager)"
ghcr_token="$(aws ssm get-parameter --region "${aws_region}" --name "${parameter_prefix}/deploy/GHCR_TOKEN" --with-decryption --query 'Parameter.Value' --output text --no-cli-pager)"
[[ -n "${ghcr_username}" && -n "${ghcr_token}" ]] || fail "GHCR 인증 Parameter가 비어 있습니다."

docker_config="${temporary_directory}/docker"
install -d --owner=root --group=root --mode=0700 "${docker_config}"
printf '%s' "${ghcr_token}" | docker --config "${docker_config}" login ghcr.io --username "${ghcr_username}" --password-stdin >/dev/null
unset ghcr_token
export DOCKER_CONFIG="${docker_config}"

compose_command=(docker compose --env-file "${runtime_env}" --env-file "${image_env}" --file "${compose_file}")

log "Compose 설정 확인과 Image Pull"
"${compose_command[@]}" config --quiet
"${compose_command[@]}" pull

log "Container 교체"
"${compose_command[@]}" up --detach --remove-orphans
wait_for_health "${health_timeout}" "${services[@]}"

if [[ "${scope}" == "app" ]]; then
  verify_image frontend "${frontend_image}" "${frontend_digest}"
  verify_image backend "${backend_image}" "${backend_digest}"

  curl --fail --silent --show-error --max-time 5 http://127.0.0.1:3000/api/health >/dev/null
  curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8080/api/actuator/health >/dev/null

  log "Nginx 설정 적용"
  [[ -f "${nginx_target}" ]] && cp --preserve=mode,ownership "${nginx_target}" "${temporary_directory}/nginx.conf.previous"
  [[ -L "${nginx_enabled}" ]] && : > "${temporary_directory}/yeodam-enabled"
  [[ -L "${maintenance_enabled}" ]] && : > "${temporary_directory}/maintenance-enabled"
  nginx_restore_required="true"

  install --owner=root --group=root --mode=0644 "${nginx_source}" "${nginx_target}"
  ln -sfn "${nginx_target}" "${nginx_enabled}"
  rm -f -- "${maintenance_enabled}"
  nginx -t
  systemctl reload nginx
  curl --fail --silent --show-error --max-time 10 \
    --resolve yeodam-2gether.com:443:127.0.0.1 \
    https://yeodam-2gether.com/api/actuator/health >/dev/null
  nginx_restore_required="false"
else
  verify_image ai-worker "${ai_image}" "${ai_digest}"
  curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8000/health >/dev/null
fi

log "성공 Release 기록"
record_release_state

docker --config "${docker_config}" logout ghcr.io >/dev/null 2>&1 || true
unset ghcr_username DOCKER_CONFIG

echo
echo "배포 완료: scope=${scope}, release=${release_dir}"
echo "Parameter와 GHCR Credential 값은 출력하지 않았습니다."
