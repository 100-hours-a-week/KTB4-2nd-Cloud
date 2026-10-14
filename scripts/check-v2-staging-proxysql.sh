#!/usr/bin/env bash
set -Eeuo pipefail
set +x

readonly directory=/etc/yeodam/v2-staging-proxysql
readonly parameter=/yeodam/v2/staging/v1-source/app/MYSQL_PASSWORD

fail() { echo "ERROR: $*" >&2; exit 1; }
[[ "${EUID}" -eq 0 ]] || fail 'Run as root on the V1 staging App host.'
[[ $# -eq 1 && "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail 'Pass the V2 staging MySQL private IPv4 address.'
[[ -f "$directory/admin-password" ]] || fail 'ProxySQL admin credential is missing.'
command -v mysql >/dev/null || fail 'MySQL client is required.'
backend_ids=$(docker ps --filter label=com.docker.compose.project=yeodam-app \
  --filter label=com.docker.compose.service=backend --quiet)
[[ -n "$backend_ids" && "$backend_ids" != *$'\n'* ]] || fail 'Expected one running V1 Backend container.'
if docker exec "$backend_ids" sh -c 'printf %s "$MYSQL_URL"' | grep -qi 'proxysql'; then
  fail 'V1 Backend already uses ProxySQL; candidate-route check could affect live requests.'
fi

v2_mysql_ip=$1
app_password=$(aws ssm get-parameter --region ap-northeast-2 --name "$parameter" --with-decryption --query Parameter.Value --output text --no-cli-pager)
admin_password=$(cat "$directory/admin-password")
[[ "$app_password" =~ ^[0-9a-fA-F]{64}$ && "$admin_password" =~ ^[0-9a-fA-F]{64}$ ]] || fail 'Invalid credential format.'

app_query() {
  local host=$1 port=$2
  MYSQL_PWD="$app_password" mysql --protocol=tcp --host="$host" --port="$port" \
    --user=yeodam_app --connect-timeout=5 --batch --skip-column-names \
    --execute='SELECT @@server_uuid'
}
admin_query() {
  MYSQL_PWD="$admin_password" mysql --protocol=tcp --host=127.0.0.1 --port=16032 \
    --user=radmin --connect-timeout=5 --batch --skip-column-names --execute="$1"
}
restore_source() {
  admin_query 'UPDATE mysql_users SET default_hostgroup=10 WHERE username="yeodam_app"; LOAD MYSQL USERS TO RUNTIME;' >/dev/null
}
trap 'restore_source || true; unset app_password admin_password' EXIT

source_uuid=$(mysql --protocol=socket --user=root --batch --skip-column-names --execute='SELECT @@server_uuid')
target_uuid=$(app_query "$v2_mysql_ip" 3306)
[[ "$source_uuid" != "$target_uuid" ]] || fail 'Source and candidate identify as the same MySQL server.'
[[ "$(admin_query 'SELECT default_hostgroup FROM mysql_users WHERE username="yeodam_app"')" == 10 ]] || fail 'ProxySQL is not routing to V1 by default.'
[[ "$(app_query 127.0.0.1 16033)" == "$source_uuid" ]] || fail 'ProxySQL default path did not reach V1 MySQL.'

# No application uses this proxy yet. Change only runtime state for one read-only
# candidate check, then restore the V1 hostgroup even when the check fails.
admin_query 'UPDATE mysql_users SET default_hostgroup=20 WHERE username="yeodam_app"; LOAD MYSQL USERS TO RUNTIME;' >/dev/null
[[ "$(app_query 127.0.0.1 16033)" == "$target_uuid" ]] || fail 'ProxySQL candidate path did not reach V2 MySQL.'
restore_source
[[ "$(app_query 127.0.0.1 16033)" == "$source_uuid" ]] || fail 'ProxySQL did not return to V1 MySQL.'

echo 'ProxySQL read-only route checks passed: V1 default -> V2 candidate -> V1 default.'
echo "V1 UUID: $source_uuid"
echo "V2 UUID: $target_uuid"
