#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

[[ $EUID -eq 0 ]] || { echo 'Run on the staging MySQL host as root' >&2; exit 1; }
[[ ${CDC_KAFKA_HOST:-} =~ ^10\.30\.10\.[0-9]{1,3}$ ]] || exit 1
[[ ${SSM_OutboxTable:-} =~ ^[A-Za-z][A-Za-z0-9_]{0,63}$ ]] || exit 1
mysql_root() {
  docker exec -i yeodam-v2-mysql sh -ec \
    'export MYSQL_PWD=$(cat /run/secrets/mysql-root-password); exec mysql -uroot --batch --skip-column-names'
}

# Do not change the replication experiment's global read-only flags implicitly.
flags=$(printf 'SELECT @@GLOBAL.read_only,@@GLOBAL.super_read_only;\n' | mysql_root)
[[ $flags == $'0\t0' ]] || { echo 'MySQL is read-only; account provisioning requires a separate DB-role decision' >&2; exit 1; }
columns=$(printf "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='yeodam_validation' AND TABLE_NAME='%s' AND COLUMN_NAME IN ('id','aggregatetype','aggregateid','type','payload');\n" "$SSM_OutboxTable" | mysql_root)
[[ $columns == 5 ]] || { echo 'Confirmed Backend five-column Outbox is not present; no account or table changed' >&2; exit 1; }

parameter=/yeodam/v2/staging/kafka/MYSQL_CDC_PASSWORD
error_file=$(mktemp)
trap 'rm -f "$error_file"; unset password' EXIT
if password=$(aws ssm get-parameter --region ap-northeast-2 --name "$parameter" \
    --with-decryption --query Parameter.Value --output text 2>"$error_file"); then
  :
elif grep -q ParameterNotFound "$error_file"; then
  password=$(openssl rand -hex 16)
  printf '%s' "$password" | aws ssm put-parameter --region ap-northeast-2 \
    --name "$parameter" --type SecureString --value file:///dev/stdin >/dev/null
else
  echo 'Could not read the dedicated CDC password' >&2
  exit 1
fi
[[ $password =~ ^[0-9a-f]{32}$ ]] || { echo 'Unexpected CDC password format' >&2; exit 1; }

# Keep account DDL out of binlog and never grant writes or SELECT on other schemas.
printf "SET SESSION sql_log_bin=0;\nCREATE USER IF NOT EXISTS 'yeodam_outbox_cdc'@'%s' IDENTIFIED BY '%s' REQUIRE SSL;\nALTER USER 'yeodam_outbox_cdc'@'%s' IDENTIFIED BY '%s' REQUIRE SSL;\nGRANT REPLICATION SLAVE,REPLICATION CLIENT ON *.* TO 'yeodam_outbox_cdc'@'%s';\nGRANT SELECT ON yeodam_validation.%s TO 'yeodam_outbox_cdc'@'%s';\n" \
  "$CDC_KAFKA_HOST" "$password" "$CDC_KAFKA_HOST" "$password" \
  "$CDC_KAFKA_HOST" "$SSM_OutboxTable" "$CDC_KAFKA_HOST" | mysql_root
unset password
printf "SHOW GRANTS FOR 'yeodam_outbox_cdc'@'%s';\n" "$CDC_KAFKA_HOST" | mysql_root
echo 'Outbox CDC account provisioned; no DB route, table, or read-only flag changed'
