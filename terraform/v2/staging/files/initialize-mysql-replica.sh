#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

region=ap-northeast-2
mysql_container=yeodam-v2-mysql
channel=v1_to_v2
source_schema=yeodam_v1_source
destination_schema=yeodam_v1_source
password_parameter=/yeodam/v2/staging/replication/MYSQL_SOURCE_PASSWORD
ca_parameter=/yeodam/v2/staging/replication/SOURCE_CA_PEM

[[ "${BACKUP_BUCKET:-}" =~ ^yeodam-v2-staging-mysql-backup-[0-9]{12}-ap-northeast-2$ ]] || {
  echo 'Unexpected staging backup bucket' >&2; exit 1;
}
[[ "${SOURCE_HOST:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo 'SOURCE_HOST must be a private IPv4 address' >&2; exit 1;
}
[[ "${SNAPSHOT_READY_KEY:-}" =~ ^migration/v1-source/[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\.ready$ ]] || {
  echo 'Unexpected source snapshot marker key' >&2; exit 1;
}
[[ "${CONFIRM_DESTINATION_RESET:-}" == RESET_STAGING_V1_COMPAT_SCHEMA_AFTER_BACKUP ]] || {
  echo 'Explicit destination reset confirmation is required' >&2; exit 1;
}
unit=/etc/systemd/system/yeodam-mysql.service
if grep -Fq -- '--replicate-rewrite-db=' "$unit"; then
  echo 'A schema rewrite must not be configured for the V1-compatible replica' >&2; exit 1;
fi
grep -Fq -- '--replicate-wild-do-table=v1_to_v2:yeodam_v1_source.%' "$unit" || {
  echo 'Persistent V1-compatible table filter is missing' >&2; exit 1;
}

temp_dir=$(mktemp -d)
container_dir=
target_mutable=0
mysql_root() {
  sudo docker exec -i "$mysql_container" sh -ec \
    'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot --binary-mode --batch --skip-column-names'
}
cleanup() {
  if [[ "$target_mutable" == 1 ]]; then
    printf 'SET GLOBAL super_read_only=ON;\n' | mysql_root >/dev/null 2>&1 || true
  fi
  if [[ -n "$container_dir" ]]; then
    sudo docker exec "$mysql_container" rm -rf "$container_dir" >/dev/null 2>&1 || true
  fi
  rm -rf "$temp_dir"
}
trap cleanup EXIT

target_identity=$(printf 'SELECT @@server_uuid, @@server_id, @@gtid_mode;\n' | mysql_root)
target_uuid=$(printf '%s\n' "$target_identity" | awk '{print $1}')
target_id=$(printf '%s\n' "$target_identity" | awk '{print $2}')
target_gtid_mode=$(printf '%s\n' "$target_identity" | awk '{print $3}')
[[ "$target_uuid" =~ ^[0-9a-f-]{36}$ && "$target_id" == 2 && "$target_gtid_mode" == ON ]] || {
  echo 'This is not the expected V2 staging MySQL replica' >&2; exit 1;
}
target_flags=$(printf 'SELECT @@read_only,@@super_read_only;\n' | mysql_root)
[[ "$target_flags" == $'1\t1' ]] || {
  echo 'The destination must be a protected read-only replica' >&2; exit 1;
}
persisted_fence=$(printf "SELECT VARIABLE_VALUE FROM performance_schema.persisted_variables WHERE VARIABLE_NAME='super_read_only';\n" | mysql_root)
[[ "$persisted_fence" == ON ]] || {
  echo 'The destination must restart with super_read_only=ON' >&2; exit 1;
}
other_clients=$(printf "SELECT COUNT(*) FROM information_schema.processlist WHERE USER NOT IN ('system user','event_scheduler','root') AND ID<>CONNECTION_ID();\n" | mysql_root)
[[ "$other_clients" == 0 ]] || {
  echo 'Another client is connected to the destination; isolate before initialization' >&2; exit 1;
}

channel_count=$(printf "SELECT COUNT(*) FROM performance_schema.replication_connection_configuration WHERE CHANNEL_NAME='%s';\n" "$channel" | mysql_root)
[[ "$channel_count" == 0 || "$channel_count" == 1 ]] || {
  echo 'Unexpected replication channel count' >&2; exit 1;
}
other_channels=$(printf "SELECT COUNT(*) FROM performance_schema.replication_connection_configuration WHERE CHANNEL_NAME NOT IN ('','%s');\n" "$channel" | mysql_root)
[[ "$other_channels" == 0 ]] || { echo 'An unrelated replication channel exists' >&2; exit 1; }
if [[ "$channel_count" == 1 ]]; then
  configured_source=$(printf "SELECT HOST FROM performance_schema.replication_connection_configuration WHERE CHANNEL_NAME='%s';\n" "$channel" | mysql_root)
  [[ "$configured_source" == "$SOURCE_HOST" ]] || {
    echo 'Existing channel points at an unexpected source' >&2; exit 1;
  }
fi

aws s3 cp "s3://${BACKUP_BUCKET}/${SNAPSHOT_READY_KEY}" "$temp_dir/ready" \
  --region "$region" --only-show-errors
marker_value() { awk -F= -v name="$1" '$1 == name { print $2 }' "$temp_dir/ready"; }
snapshot_key=$(marker_value snapshot_key)
marker_schema=$(marker_value source_schema)
source_uuid=$(marker_value source_uuid)
source_file=$(marker_value source_file)
source_position=$(marker_value source_position)
expected_sha=$(marker_value sha256)
expected_size=$(marker_value compressed_bytes)

[[ "$snapshot_key" == "${SNAPSHOT_READY_KEY%.ready}.sql.gz" ]] || {
  echo 'The marker does not match the selected snapshot' >&2; exit 1;
}
[[ "$marker_schema" == "$source_schema" ]] || {
  echo 'Snapshot is not from the V1 application schema' >&2; exit 1;
}
[[ "$source_uuid" =~ ^[0-9a-f-]{36}$ && "$source_uuid" != "$target_uuid" ]] || {
  echo 'Invalid or identical source and target UUIDs' >&2; exit 1;
}
[[ "$source_file" =~ ^binlog\.[0-9]+$ && "$source_position" =~ ^[0-9]+$ ]] || {
  echo 'Invalid source binary log coordinates' >&2; exit 1;
}
[[ "$expected_sha" =~ ^[0-9a-f]{64}$ && "$expected_size" =~ ^[0-9]+$ && "$expected_size" -gt 0 ]] || {
  echo 'Invalid source snapshot checksum or length' >&2; exit 1;
}

actual_size=$(aws s3api head-object --bucket "$BACKUP_BUCKET" --key "$snapshot_key" \
  --region "$region" --query ContentLength --output text)
[[ "$actual_size" == "$expected_size" ]] || {
  echo 'Source snapshot length does not match the ready marker' >&2; exit 1;
}
actual_sha=$(aws s3 cp "s3://${BACKUP_BUCKET}/${snapshot_key}" - \
  --region "$region" --only-show-errors | sha256sum | awk '{print $1}')
[[ "$actual_sha" == "$expected_sha" ]] || {
  echo 'Source snapshot SHA-256 does not match the ready marker' >&2; exit 1;
}

aws ssm get-parameter --region "$region" --name "$ca_parameter" \
  --query Parameter.Value --output text >"$temp_dir/ca.pem"
openssl x509 -in "$temp_dir/ca.pem" -noout -fingerprint -sha256
password=$(aws ssm get-parameter --region "$region" --name "$password_parameter" \
  --with-decryption --query Parameter.Value --output text)
[[ "$password" =~ ^[0-9a-fA-F]{32}$ ]] || {
  echo 'Invalid replication credential' >&2; exit 1;
}
printf '[client]\npassword=%s\n' "$password" >"$temp_dir/client.cnf"
container_dir=$(sudo docker exec "$mysql_container" mktemp -d)
sudo docker cp "$temp_dir/ca.pem" "$mysql_container:$container_dir/ca.pem" >/dev/null
sudo docker cp "$temp_dir/client.cnf" "$mysql_container:$container_dir/client.cnf" >/dev/null

live_source_uuid=$(sudo docker exec "$mysql_container" mysql \
  --defaults-extra-file="$container_dir/client.cnf" \
  --protocol=tcp --host="$SOURCE_HOST" --port=3306 --user=yeodam_repl \
  --ssl-mode=VERIFY_CA --ssl-ca="$container_dir/ca.pem" \
  --batch --skip-column-names --execute='SELECT @@server_uuid')
[[ "$live_source_uuid" == "$source_uuid" ]] || {
  echo 'Snapshot source UUID differs from the live TLS-verified source' >&2; exit 1;
}

# The existing destination contains rehearsal data. Keep a complete ready
# backup before resetting only the V1-compatible schema; this step must succeed.
destination_backup=$(sudo /usr/local/sbin/yeodam-mysql-full-backup)
[[ "$destination_backup" == *'.ready'* ]] || {
  echo 'Destination backup did not produce a ready marker' >&2; exit 1;
}
echo "$destination_backup"

# The replica is normally protected. Open only the running process after the
# backup; its persisted read-only settings protect it through both restarts.
target_mutable=1
printf 'SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=OFF;\n' | mysql_root
[[ "$(printf 'SELECT @@read_only,@@super_read_only;\n' | mysql_root)" == $'0\t0' ]] || {
  echo 'Could not open the backed-up destination for initialization' >&2; exit 1;
}

# Delete only the expected failed/old channel, after the destination backup.
# Restarting reapplies the channel-specific startup filters, which RESET
# REPLICA ALL removes together with the old channel.
if [[ "$channel_count" == 1 ]]; then
  printf "STOP REPLICA FOR CHANNEL '%s'; RESET REPLICA ALL FOR CHANNEL '%s';\n" "$channel" "$channel" | mysql_root
  sudo systemctl restart yeodam-mysql.service
  for _ in $(seq 1 60); do
    if printf 'SELECT 1;\n' | mysql_root >/dev/null 2>&1; then break; fi
    sleep 2
  done
  printf 'SELECT 1;\n' | mysql_root >/dev/null
  [[ "$(printf 'SELECT @@read_only,@@super_read_only;\n' | mysql_root)" == $'1\t1' ]] || {
    echo 'Destination did not restart read-only' >&2; exit 1;
  }
  printf 'SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=OFF;\n' | mysql_root
fi

printf 'SET SESSION sql_log_bin=0; DROP DATABASE IF EXISTS %s; CREATE DATABASE %s CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;\n' "$destination_schema" "$destination_schema" | mysql_root
{
  printf 'SET SESSION sql_log_bin=0;\n'
  printf 'USE %s;\n' "$destination_schema"
  aws s3 cp "s3://${BACKUP_BUCKET}/${snapshot_key}" - \
    --region "$region" --only-show-errors | gzip -dc
} | mysql_root

table_count=$(printf "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='%s';\n" "$destination_schema" | mysql_root)
[[ "$table_count" =~ ^[0-9]+$ && "$table_count" -gt 0 ]] || {
  echo 'The imported source schema has no tables' >&2; exit 1;
}

# The public CA is retained on the destination Data EBS and visible to the
# MySQL process through the existing /srv/yeodam/mysql bind mount.
install -m 0644 "$temp_dir/ca.pem" /srv/yeodam/mysql/source-ca.pem

# The two databases already have distinct local GTID histories. This first
# rehearsal starts from the exact binlog position in the consistent dump;
# it never rewrites either server's gtid_purged set.
printf "CHANGE REPLICATION SOURCE TO SOURCE_HOST='%s', SOURCE_PORT=3306, SOURCE_USER='yeodam_repl', SOURCE_PASSWORD='%s', SOURCE_LOG_FILE='%s', SOURCE_LOG_POS=%s, SOURCE_AUTO_POSITION=0, SOURCE_SSL=1, SOURCE_SSL_CA='/var/lib/mysql/source-ca.pem', SOURCE_SSL_VERIFY_SERVER_CERT=0 FOR CHANNEL '%s';\n" \
  "$SOURCE_HOST" "$password" "$source_file" "$source_position" "$channel" | mysql_root
unset password
# Startup channel filters are registered only when the channel already exists.
# Restart after CHANGE REPLICATION SOURCE, before applying any source events.
sudo systemctl restart yeodam-mysql.service
for _ in $(seq 1 60); do
  if printf 'SELECT 1;\n' | mysql_root >/dev/null 2>&1; then break; fi
  sleep 2
done
printf 'SELECT 1;\n' | mysql_root >/dev/null
[[ "$(printf 'SELECT @@read_only,@@super_read_only;\n' | mysql_root)" == $'1\t1' ]] || {
  echo 'Destination did not restart read-only before replication' >&2; exit 1;
}
printf "START REPLICA FOR CHANNEL '%s';\n" "$channel" | mysql_root

for _ in $(seq 1 30); do
  status=$(printf "SHOW REPLICA STATUS FOR CHANNEL '%s';\n" "$channel" \
    | sudo docker exec -i "$mysql_container" sh -ec \
        'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot --vertical')
  if grep -Eq '^ *Replica_IO_Running: Yes$' <<<"$status" \
      && grep -Eq '^ *Replica_SQL_Running: Yes$' <<<"$status" \
      && grep -Eq '^ *Last_IO_Errno: 0$' <<<"$status" \
      && grep -Eq '^ *Last_SQL_Errno: 0$' <<<"$status" \
      && grep -Eq '^ *Replicate_Rewrite_DB: *$' <<<"$status" \
      && grep -Eq '^ *Replicate_Wild_Do_Table: yeodam_v1_source[.]%$' <<<"$status"; then
    printf 'SET GLOBAL super_read_only=ON;\n' | mysql_root
    [[ "$(printf 'SELECT @@read_only,@@super_read_only;\n' | mysql_root)" == $'1\t1' ]] || {
      echo 'Destination did not return to read-only protection' >&2; exit 1;
    }
    target_mutable=0
    printf '%s\n' "$status" | grep -E '^ *(Channel_Name|Source_Host|Source_Log_File|Read_Source_Log_Pos|Replica_IO_Running|Replica_SQL_Running|Seconds_Behind_Source|Last_IO_Errno|Last_SQL_Errno|Source_SSL_CA_File):'
    echo "Imported $table_count source tables; replication channel $channel is running."
    exit 0
  fi
  sleep 2
done

echo 'Replication did not become healthy; the destination backup remains available' >&2
printf '%s\n' "$status" | grep -E '^ *(Channel_Name|Replica_IO_Running|Replica_SQL_Running|Last_IO_Errno|Last_IO_Error|Last_SQL_Errno|Last_SQL_Error):' >&2 || true
exit 1
