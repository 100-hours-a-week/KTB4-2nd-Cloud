#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

readonly directory=/etc/yeodam/v2-staging-proxysql
readonly data_directory=/var/lib/yeodam-v2-staging-proxysql
readonly parameter=/yeodam/v2/staging/v1-source/app/MYSQL_PASSWORD
readonly compose_file="$directory/compose.yml"

fail() { echo "ERROR: $*" >&2; exit 1; }

[[ "${EUID}" -eq 0 ]] || fail 'Run as root on the V1 staging App host.'
[[ $# -eq 1 && "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail 'Pass the V2 staging MySQL private IPv4 address.'
[[ -f "$compose_file" ]] || fail "Missing $compose_file"
command -v docker >/dev/null || fail 'Docker is required.'
command -v mysql >/dev/null || fail 'MySQL client is required.'
docker network inspect yeodam-app-network >/dev/null || fail 'Deploy the V1 staging App network first.'
[[ ! -e "$data_directory/proxysql.db" ]] || fail 'ProxySQL already has a persistent configuration; inspect it instead of overwriting bootstrap config.'

v2_mysql_ip=$1
password=$(aws ssm get-parameter --region ap-northeast-2 --name "$parameter" --with-decryption --query Parameter.Value --output text --no-cli-pager)
[[ "$password" =~ ^[0-9a-fA-F]{64}$ ]] || fail 'Invalid V1 staging MySQL password format.'
mysql --protocol=socket --user=root --batch --skip-column-names \
  --execute='SELECT @@server_uuid FROM yeodam_v1_source.users LIMIT 1' >/dev/null ||
  fail 'Cannot read the local V1 source schema.'
MYSQL_PWD="$password" mysql --protocol=tcp --host="$v2_mysql_ip" --port=3306 \
  --user=yeodam_app --connect-timeout=5 --batch --skip-column-names \
  --execute='SELECT @@server_uuid FROM yeodam_v1_source.users LIMIT 1' >/dev/null ||
  fail 'Cannot read the V2 candidate schema with the V1 App account.'
admin_password=$(openssl rand -hex 32)
install -d -o root -g root -m 0700 "$directory" "$data_directory"

temporary=$(mktemp "$directory/proxysql.cnf.XXXXXX")
trap 'rm -f "$temporary"; unset password admin_password' EXIT
cat > "$temporary" <<EOF
datadir="/var/lib/proxysql"

admin_variables=
{
  admin_credentials="admin:${admin_password};radmin:${admin_password}"
  mysql_ifaces="0.0.0.0:6032"
}

mysql_variables=
{
  interfaces="0.0.0.0:6033"
  default_schema="yeodam_v1_source"
  monitor_enabled=false
  max_connections=2048
}

mysql_servers=
(
  { address="172.28.0.1", port=3306, hostgroup=10, max_connections=200 },
  { address="${v2_mysql_ip}", port=3306, hostgroup=20, max_connections=200 }
)

mysql_users=
(
  { username="yeodam_app", password="${password}", default_hostgroup=10,
    default_schema="yeodam_v1_source", max_connections=1000, active=1 }
)
EOF
install -o root -g root -m 0600 "$temporary" "$directory/proxysql.cnf"
printf '%s\n' "$admin_password" > "$directory/admin-password"
chmod 0600 "$directory/admin-password"
unset password admin_password

docker compose -f "$compose_file" config --quiet
docker compose -f "$compose_file" pull
docker compose -f "$compose_file" up --detach
echo 'ProxySQL started with V1 hostgroup 10 as default; V2 hostgroup 20 has no client routing.'
echo 'The V1 Backend MYSQL_URL has not changed.'
