#!/bin/sh
set +x
set -eu

region=ap-northeast-2
password_parameter=/yeodam/v2/staging/replication/MYSQL_SOURCE_PASSWORD
ca_parameter=/yeodam/v2/staging/replication/SOURCE_CA_PEM
mysql_container=yeodam-v2-mysql
replication_user=yeodam_repl

case "${SOURCE_HOST:-}" in
  *[!0-9.]*|'') echo 'SOURCE_HOST must be an IPv4 address' >&2; exit 1 ;;
esac
case "${REPLICA_HOST:-}" in
  *[!0-9.]*|'') echo 'REPLICA_HOST must be an IPv4 address' >&2; exit 1 ;;
esac

temp_dir=$(mktemp -d)
container_dir=
cleanup() {
  if [ -n "$container_dir" ]; then
    sudo docker exec "$mysql_container" rm -rf "$container_dir" >/dev/null 2>&1 || true
  fi
  rm -rf "$temp_dir"
}
trap cleanup EXIT HUP INT TERM
umask 077

aws ssm get-parameter --region "$region" --name "$ca_parameter" \
  --query Parameter.Value --output text >"$temp_dir/ca.pem"
openssl x509 -in "$temp_dir/ca.pem" -noout -fingerprint -sha256

password=$(aws ssm get-parameter \
  --region "$region" --name "$password_parameter" --with-decryption \
  --query Parameter.Value --output text)
case "$password" in
  *[!0-9a-fA-F]*|'') echo 'Replication password must be hexadecimal' >&2; exit 1 ;;
esac
[ "${#password}" -eq 32 ] || { echo 'Replication password must be 32 hex characters' >&2; exit 1; }
printf '[client]\npassword=%s\n' "$password" >"$temp_dir/client.cnf"
unset password

container_dir=$(sudo docker exec "$mysql_container" mktemp -d)
sudo docker cp "$temp_dir/ca.pem" "$mysql_container:$container_dir/ca.pem" >/dev/null
sudo docker cp "$temp_dir/client.cnf" "$mysql_container:$container_dir/client.cnf" >/dev/null

result=$(sudo docker exec "$mysql_container" mysql \
  --defaults-extra-file="$container_dir/client.cnf" \
  --protocol=tcp --host="$SOURCE_HOST" --port=3306 --user="$replication_user" \
  --ssl-mode=VERIFY_CA --ssl-ca="$container_dir/ca.pem" \
  --batch --skip-column-names \
  --execute="SHOW STATUS LIKE 'Ssl_cipher'; SELECT CURRENT_USER()")

cipher=$(printf '%s\n' "$result" | awk '$1 == "Ssl_cipher" { print $2 }')
[ -n "$cipher" ] || { echo 'Verified TLS connection did not negotiate a cipher' >&2; exit 1; }
printf '%s\n' "$result" | grep -Fqx "${replication_user}@${REPLICA_HOST}" || {
  echo 'Connection did not match the exact replication account host' >&2
  exit 1
}

if sudo docker exec "$mysql_container" mysql \
  --defaults-extra-file="$container_dir/client.cnf" \
  --protocol=tcp --host="$SOURCE_HOST" --port=3306 --user="$replication_user" \
  --ssl-mode=DISABLED --get-server-public-key \
  --execute='SELECT 1' >/dev/null 2>&1; then
  echo 'Unexpected plaintext replication account login succeeded' >&2
  exit 1
fi

printf 'Verified source CA and negotiated TLS cipher: %s\n' "$cipher"
printf 'Connected as %s@%s; plaintext login was rejected.\n' "$replication_user" "$REPLICA_HOST"
