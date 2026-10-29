#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly DOMAIN='v1-staging.yeodam-2gether.com'
readonly AWS_REGION='ap-northeast-2'
readonly PARAMETER_PREFIX='/yeodam/v2/staging/v1-source'
readonly BACKEND_LOG_GROUP='/yeodam/v2/staging/v1-source/backend'
readonly AI_LOG_GROUP='/yeodam/v2/staging/v1-source/ai'

scope=''
release_dir=''
image_env=''
redis_compat='false'
user_read_limit='false'
temporary_directory=''

fail() { echo "ERROR: $*" >&2; exit 1; }
image_value() {
  local key="$1"
  awk -F= -v key="${key}" '$1 == key { count++; value = substr($0, index($0, "=") + 1) } END { if (count == 1) print value }' "${image_env}"
}
require_image() {
  local image_key="$1" digest_key="$2" package="$3" image digest
  image="$(image_value "${image_key}")"
  digest="$(image_value "${digest_key}")"
  if [[ "${package}" == yeodam-backend && "${image}" =~ ^ghcr\.io/100-hours-a-week/yeodam-backend:migration-probe-v1-sha-[0-9a-f]{40}$ ]]; then
    : # BE's dedicated V1 rehearsal tag is accepted only by this staging deploy script.
  else
    [[ "${image}" =~ ^ghcr\.io/100-hours-a-week/${package}:sha-[0-9a-f]{40}$ ]] || fail "Invalid ${image_key}."
  fi
  [[ "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "Invalid ${digest_key}."
}
cleanup() {
  if [[ -n "${temporary_directory}" && -d "${temporary_directory}" ]]; then
    rm -rf -- "${temporary_directory}"
  fi
}
trap cleanup EXIT

usage() {
  cat <<'EOF'
Usage: sudo ./scripts/deploy-v1-staging-source.sh \
  --scope app|worker \
  --release-dir /opt/yeodam/releases/CLOUD_COMMIT_SHA \
  --image-env /etc/yeodam/v1-source-images.env [--redis-compat] [--user-read-limit]

Runtime values are read only from /yeodam/v2/staging/v1-source in Parameter Store.
The supplied image file must contain staging-compatible SHA and digest references.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope) scope="${2:?missing scope}"; shift 2 ;;
    --release-dir) release_dir="${2:?missing release directory}"; shift 2 ;;
    --image-env) image_env="${2:?missing image env file}"; shift 2 ;;
    --redis-compat) redis_compat='true'; shift ;;
    --user-read-limit) user_read_limit='true'; shift ;;
    --help|-h) usage; exit 0 ;;
    *) fail "Unknown argument: $1" ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || fail 'Run as root.'
[[ "${scope}" == app || "${scope}" == worker ]] || fail 'Scope must be app or worker.'
[[ "${user_read_limit}" != true || "${scope}" == app ]] || fail 'User read limiting is App-only.'
if [[ "${scope}" == app ]]; then
  [[ -f "$(dirname -- "${BASH_SOURCE[0]}")/render-v1-staging-nginx.py" ]] || fail 'Staging Nginx renderer is missing.'
  [[ -f "$(dirname -- "${BASH_SOURCE[0]}")/../monitoring/v1-staging/nginx_observability.py" ]] || fail 'Staging Nginx observation helper is missing.'
fi
[[ "${release_dir}" == /opt/yeodam/releases/* ]] || fail 'Release must be under /opt/yeodam/releases.'
[[ -f "${image_env}" ]] || fail "Image file does not exist: ${image_env}"
[[ "$(stat -c %a "${image_env}")" == 600 ]] || fail 'Image file mode must be 600.'
[[ "$(stat -c %u "${image_env}")" == 0 ]] || fail 'Image file must be owned by root.'
release_dir="$(realpath -e -- "${release_dir}")"
[[ "${release_dir}" == /opt/yeodam/releases/* ]] || fail 'Release symlink escapes release root.'
[[ -f "${release_dir}/scripts/render-runtime-env.sh" ]] || fail 'Runtime renderer is missing.'
[[ -f "${release_dir}/app-compose.yml" && -f "${release_dir}/worker-compose.yml" ]] || fail 'Compose files are missing.'

temporary_directory="$(mktemp -d /run/yeodam-v1-source.XXXXXX)"
runtime_env="${temporary_directory}/${scope}.env"
docker_config="${temporary_directory}/docker"
install -d -m 0700 "${docker_config}"

bash "${release_dir}/scripts/render-runtime-env.sh" \
  --scope "${scope}" \
  --region "${AWS_REGION}" \
  --path-prefix "${PARAMETER_PREFIX}" \
  --output "${runtime_env}"

if [[ "${scope}" == app ]]; then
  [[ "$(image_value FRONTEND_API_ORIGIN)" == "https://${DOMAIN}" ]] || fail 'FRONTEND_API_ORIGIN must explicitly attest the staging FE build.'
  require_image FRONTEND_IMAGE FRONTEND_DIGEST yeodam-frontend
  require_image BACKEND_IMAGE BACKEND_DIGEST yeodam-backend
  if [[ "${user_read_limit}" == true ]]; then
    [[ "$(image_value BACKEND_DIGEST)" == sha256:3f1cb707caa276a7b834d92279d3738184f0b7c8b681e876544f0cc9612dc2b8 ]] || fail 'User read limiting requires the reviewed V1 image digest.'
    printf 'V1_USER_READ_RATE_LIMIT_ENABLED=true\nV1_USER_READ_RATE_LIMIT_RPS=5\nV1_USER_READ_RATE_LIMIT_BURST=30\nV1_USER_READ_RATE_LIMIT_MAX_USERS=10000\n' >> "${runtime_env}"
  fi
  printf 'BACKEND_LOG_GROUP=%s\n' "${BACKEND_LOG_GROUP}" >> "${runtime_env}"
  compose_files=(-f "${release_dir}/app-compose.yml")
  services=(backend frontend)
  if [[ "${redis_compat}" == true ]]; then
    [[ -f "${release_dir}/deploy/v1-staging-compose.override.yml" ]] || fail 'Staging Compose override is missing.'
    compose_files+=(-f "${release_dir}/deploy/v1-staging-compose.override.yml")
    services=(redis backend frontend)
    redis_image="$(image_value V1_SOURCE_REDIS_IMAGE)"
    [[ "${redis_image}" =~ ^(docker\.io/library/)?redis:[^@]+@sha256:[0-9a-f]{64}$ ]] || fail 'V1_SOURCE_REDIS_IMAGE must include a digest.'
  fi
  [[ -f "${release_dir}/nginx/yeodam.conf" ]] || fail 'Nginx template is missing.'
  [[ -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]] || fail 'Staging TLS certificate is missing.'
else
  require_image AI_IMAGE AI_DIGEST yeodam-ai-worker
  printf 'AI_LOG_GROUP=%s\n' "${AI_LOG_GROUP}" >> "${runtime_env}"
  compose_files=(-f "${release_dir}/worker-compose.yml")
  services=(ai-worker)
fi

ghcr_username="$(aws ssm get-parameter --region "${AWS_REGION}" --name "${PARAMETER_PREFIX}/deploy/GHCR_USERNAME" --query 'Parameter.Value' --output text --no-cli-pager)"
ghcr_token="$(aws ssm get-parameter --region "${AWS_REGION}" --name "${PARAMETER_PREFIX}/deploy/GHCR_TOKEN" --with-decryption --query 'Parameter.Value' --output text --no-cli-pager)"
[[ -n "${ghcr_username}" && -n "${ghcr_token}" ]] || fail 'Staging GHCR credentials are empty.'
printf %s "${ghcr_token}" | docker --config "${docker_config}" login ghcr.io --username "${ghcr_username}" --password-stdin >/dev/null
unset ghcr_token

compose=(docker --config "${docker_config}" compose --env-file "${runtime_env}" --env-file "${image_env}" "${compose_files[@]}")
"${compose[@]}" config --quiet
"${compose[@]}" pull
"${compose[@]}" up --detach --remove-orphans

deadline=$((SECONDS + 240))
for service in "${services[@]}"; do
  while ((SECONDS < deadline)); do
    container_id="$("${compose[@]}" ps --quiet "${service}")"
    if [[ -n "${container_id}" && "$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}")" == healthy ]]; then
      break
    fi
    sleep 5
  done
  ((SECONDS < deadline)) || fail "${service} did not become healthy."
done

if [[ "${scope}" == app ]]; then
  nginx_rendered="${temporary_directory}/nginx.conf"
  user_read_args=()
  if [[ "${user_read_limit}" == true ]]; then
    # Verify the healthy runtime, not merely the requested deployment flag.
    backend_id="$("${compose[@]}" ps --quiet backend)"
    docker inspect --format '{{range .Config.Env}}{{if eq . "V1_USER_READ_RATE_LIMIT_ENABLED=true"}}enabled{{end}}{{end}}' "${backend_id}" | grep -qx enabled || fail 'Backend user read limiter is not active.'
    user_read_args=(--user-read-limit-enabled --backend-digest "$(image_value BACKEND_DIGEST)")
  fi
  python3 "$(dirname -- "${BASH_SOURCE[0]}")/render-v1-staging-nginx.py" \
    --template "${release_dir}/nginx/yeodam.conf" --output "${nginx_rendered}" \
    --release "${release_dir##*/}" "${user_read_args[@]}"
  install -m 0644 "${nginx_rendered}" /etc/nginx/sites-available/yeodam-v1-staging
  ln -sfn /etc/nginx/sites-available/yeodam-v1-staging /etc/nginx/sites-enabled/yeodam-v1-staging
  rm -f /etc/nginx/sites-enabled/yeodam-maintenance
  rm -f /etc/nginx/sites-enabled/yeodam-v1-staging-acme
  nginx -t
  systemctl reload nginx
  curl --fail --silent --show-error --max-time 10 \
    --resolve "${DOMAIN}:443:127.0.0.1" \
    "https://${DOMAIN}/api/actuator/health" >/dev/null
fi

docker --config "${docker_config}" logout ghcr.io >/dev/null 2>&1 || true
echo "V1 staging source deployed: ${scope}"
