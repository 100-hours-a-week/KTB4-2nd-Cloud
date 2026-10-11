#!/bin/sh
set +x
set -eu

region=ap-northeast-2
password_parameter=/yeodam/v2/staging/replication/MYSQL_SOURCE_PASSWORD
ca_parameter=/yeodam/v2/staging/replication/SOURCE_CA_PEM
replication_user=yeodam_repl
ca_file=/var/lib/mysql/ca.pem

case "${REPLICA_HOST:-}" in
  *[!0-9.]*|'') echo 'REPLICA_HOST must be an IPv4 address' >&2; exit 1 ;;
esac
test "$(printf '%s' "$REPLICA_HOST" | tr -cd . | wc -c | tr -d ' ')" -eq 3 || {
  echo 'REPLICA_HOST must be an IPv4 address' >&2
  exit 1
}
test -s "$ca_file" || { echo 'MySQL source CA certificate is missing' >&2; exit 1; }

error_file=$(mktemp)
trap 'rm -f "$error_file"' EXIT HUP INT TERM

if password=$(aws ssm get-parameter \
  --region "$region" --name "$password_parameter" --with-decryption \
  --query Parameter.Value --output text 2>"$error_file"); then
  :
elif grep -q 'ParameterNotFound' "$error_file"; then
  password=$(openssl rand -hex 32)
  printf '%s' "$password" | aws ssm put-parameter \
    --region "$region" --name "$password_parameter" --type SecureString \
    --value file:///dev/stdin --query Version --output text >/dev/null
else
  echo 'Could not read the replication password parameter' >&2
  exit 1
fi

case "$password" in
  *[!0-9a-fA-F]*|'') echo 'Replication password must be hexadecimal' >&2; exit 1 ;;
esac
[ "${#password}" -eq 64 ] || { echo 'Replication password must be 64 hex characters' >&2; exit 1; }

# Do not put the password in the SSM document, command arguments or output.
# Account management statements must not be copied into the source binlog.
printf "SET SESSION sql_log_bin=0;\nCREATE USER IF NOT EXISTS '%s'@'%s' IDENTIFIED BY '%s' REQUIRE SSL;\nALTER USER '%s'@'%s' IDENTIFIED BY '%s' REQUIRE SSL;\nGRANT REPLICATION SLAVE ON *.* TO '%s'@'%s';\n" \
  "$replication_user" "$REPLICA_HOST" "$password" \
  "$replication_user" "$REPLICA_HOST" "$password" \
  "$replication_user" "$REPLICA_HOST" | sudo mysql --protocol=socket -uroot
unset password

# The CA is a public certificate. Refreshing this parameter after a source
# rebuild keeps the probe and future replica configuration tied to that source.
aws ssm put-parameter \
  --region "$region" --name "$ca_parameter" --type String \
  --value "file://$ca_file" --overwrite --query Version --output text >/dev/null

sudo mysql --protocol=socket -uroot -NBe \
  "SHOW GRANTS FOR '${replication_user}'@'${REPLICA_HOST}'"
openssl x509 -in "$ca_file" -noout -fingerprint -sha256
echo 'V1 staging replication account and source CA are ready.'
