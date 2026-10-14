#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

readonly region=ap-northeast-2
readonly domain=v1-staging.yeodam-2gether.com
readonly parameter_prefix=/yeodam/v2/staging/v1-source
readonly direct_url=jdbc:mysql://host.docker.internal:3306/yeodam_v1_source
readonly proxy_url=jdbc:mysql://proxysql:6033/yeodam_v1_source

fail() { echo "ERROR: $*" >&2; exit 1; }
usage() {
  echo 'Usage: switch-v1-staging-be-db-route.sh --route direct|proxysql --release-dir /opt/yeodam/releases/SHA --image-env /etc/yeodam/v1-source-images.env' >&2
  exit 2
}

route=''
release_dir=''
image_env=''
while (($#)); do
  case "$1" in
    --route) route=${2:-}; shift 2 ;;
    --release-dir) release_dir=${2:-}; shift 2 ;;
    --image-env) image_env=${2:-}; shift 2 ;;
    *) usage ;;
  esac
done
[[ $EUID -eq 0 ]] || fail 'Run as root on the V1 staging App host.'
[[ $route == direct || $route == proxysql ]] || usage
[[ $release_dir == /opt/yeodam/releases/* && -f $release_dir/app-compose.yml ]] || usage
[[ $image_env == /etc/yeodam/* && -f $image_env ]] || usage
[[ $(stat -c %a "$image_env") == 600 && $(stat -c %u "$image_env") == 0 ]] || fail 'Image file must be root-owned with mode 600.'
release_dir=$(realpath -e -- "$release_dir")
[[ $release_dir == /opt/yeodam/releases/* ]] || fail 'Release directory escapes release root.'
[[ -f $release_dir/scripts/render-runtime-env.sh ]] || fail 'Runtime renderer is missing.'

backend_id=$(docker ps --filter label=com.docker.compose.project=yeodam-app \
  --filter label=com.docker.compose.service=backend --quiet)
[[ -n $backend_id && $backend_id != *$'\n'* ]] || fail 'Expected exactly one running V1 Backend.'
expected_image=$(awk -F= '$1 == "BACKEND_IMAGE" { image=$2 } $1 == "BACKEND_DIGEST" { digest=$2 } END { if (image != "" && digest != "") print image "@" digest }' "$image_env")
current_image=$(docker inspect "$backend_id" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["Config"]["Image"])')
[[ -n $expected_image && $current_image == "$expected_image" ]] || fail 'Image file does not match the running Backend image.'
current_url=$(docker exec "$backend_id" sh -c 'printf %s "$MYSQL_URL"')
[[ $current_url == "$direct_url" || $current_url == "$proxy_url" ]] || fail 'Current Backend DB URL is not one of the expected staging routes.'
target_url=$direct_url
[[ $route == proxysql ]] && target_url=$proxy_url

parameter_url=$(aws ssm get-parameter --region "$region" --name "$parameter_prefix/app/MYSQL_URL" \
  --query Parameter.Value --output text --no-cli-pager)
[[ $parameter_url == "$direct_url" ]] || fail 'Staging Parameter Store direct DB URL has changed; inspect before switching.'

if [[ $route == proxysql ]]; then
  proxy_id=$(docker ps --filter label=com.docker.compose.project=yeodam-v2-staging-proxysql \
    --filter label=com.docker.compose.service=proxysql --quiet)
  [[ -n $proxy_id && $proxy_id != *$'\n'* ]] || fail 'Expected exactly one running ProxySQL container.'
  [[ $(docker inspect "$proxy_id" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["State"]["Status"])') == running ]] || fail 'ProxySQL is not running.'
  admin_password=$(cat /etc/yeodam/v2-staging-proxysql/admin-password)
  hostgroup=$(MYSQL_PWD="$admin_password" mysql --protocol=tcp -h 127.0.0.1 -P 16032 -u radmin \
    --connect-timeout=5 --batch --skip-column-names \
    -e 'SELECT DISTINCT default_hostgroup FROM runtime_mysql_users WHERE username="yeodam_app"')
  unset admin_password
  [[ $hostgroup == 10 ]] || fail 'ProxySQL must still route the app account to V1 hostgroup 10.'
  app_password=$(aws ssm get-parameter --region "$region" --name "$parameter_prefix/app/MYSQL_PASSWORD" \
    --with-decryption --query Parameter.Value --output text --no-cli-pager)
  source_uuid=$(mysql --protocol=socket -u root --batch --skip-column-names -e 'SELECT @@server_uuid')
  routed_uuid=$(MYSQL_PWD="$app_password" mysql --protocol=tcp -h 127.0.0.1 -P 16033 -u yeodam_app \
    --connect-timeout=5 --batch --skip-column-names -e 'SELECT @@server_uuid')
  unset app_password
  [[ $routed_uuid == "$source_uuid" ]] || fail 'ProxySQL does not currently reach the V1 source MySQL.'
fi

if [[ $current_url == "$target_url" ]]; then
  echo "Backend already uses route: $route"
  exit 0
fi

temporary_directory=$(mktemp -d /run/yeodam-db-route.XXXXXX)
switch_started=false
cleanup() {
  local result=$?
  trap - EXIT
  if [[ $result -ne 0 && $switch_started == true ]]; then
    sed -i "s|^MYSQL_URL=.*$|MYSQL_URL='$current_url'|" "$runtime_env"
    if "${compose[@]}" up --detach --no-deps backend >/dev/null; then
      echo 'Redeployed the previous Backend DB route after failure; verify Backend health.' >&2
    else
      echo 'Automatic route restoration failed; restore the direct route immediately.' >&2
    fi
  fi
  rm -rf -- "$temporary_directory"
}
trap cleanup EXIT
runtime_env=$temporary_directory/app.env
bash "$release_dir/scripts/render-runtime-env.sh" --scope app --region "$region" \
  --path-prefix "$parameter_prefix" --output "$runtime_env" >/dev/null
[[ $(stat -c %a "$runtime_env") == 600 ]] || fail 'Runtime env file permissions are too broad.'
sed -i "s|^MYSQL_URL=.*$|MYSQL_URL='$target_url'|" "$runtime_env"
printf 'BACKEND_LOG_GROUP=/yeodam/v2/staging/v1-source/backend\n' >> "$runtime_env"

compose=(docker compose --env-file "$runtime_env" --env-file "$image_env" -f "$release_dir/app-compose.yml")
"${compose[@]}" config --quiet
switch_started=true
"${compose[@]}" up --detach --no-deps backend

deadline=$((SECONDS + 150))
while ((SECONDS < deadline)); do
  new_id=$("${compose[@]}" ps --quiet backend)
  if [[ -n $new_id && $(docker inspect "$new_id" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["State"].get("Health",{}).get("Status",""))') == healthy ]]; then
    new_url=$(docker exec "$new_id" sh -c 'printf %s "$MYSQL_URL"')
    [[ $new_url == "$target_url" ]] || fail 'Healthy Backend has an unexpected DB URL.'
    if curl --fail --silent --show-error --max-time 10 \
      --resolve "$domain:443:127.0.0.1" "https://$domain/api/actuator/health" >/dev/null; then
      echo "Backend healthy through V1 Nginx; DB route: $route"
      exit 0
    fi
  fi
  sleep 5
done
fail "Backend did not become healthy through route: $route; run the direct route immediately."
