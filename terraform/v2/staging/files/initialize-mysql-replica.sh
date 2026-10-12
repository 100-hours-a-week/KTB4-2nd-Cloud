#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

region=ap-northeast-2
mysql_container=yeodam-v2-mysql
channel=v1_to_v2
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
[[ "${CONFIRM_DESTINATION_RESET:-}" == RESET_STAGING_YEODAM_AFTER_BACKUP ]] || {
  echo 'Explicit destination reset confirmation is required' >&2; exit 1;
}

temp_dir=$(mktemp -d)
container_dir=
cleanup() {
  if [[ -n "$container_dir" ]]; then
    sudo docker exec "$mysql_container" rm -rf "$container_dir" >/dev/null 2>&1 || true
  fi
  rm -rf "$temp_dir"
}
trap cleanup EXIT

mysql_root() {
  sudo docker exec -i "$mysql_container" sh -ec \
    'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot --binary-mode --batch --skip-column-names'
}

target_identity=$(printf 'SELECT @@server_uuid, @@server_id, @@gtid_mode;\n' | mysql_root)
target_uuid=$(printf '%s\n' "$target_identity" | awk '{print $1}')
target_id=$(printf '%s\n' "$target_identity" | awk '{print $2}')
target_gtid_mode=$(printf '%s\n' "$target_identity" | awk '{print $3}')
[[ "$target_uuid" =~ ^[0-9a-f-]{36}$ && "$target_id" == 2 && "$target_gtid_mode" == ON ]] || {
  echo 'This is not the expected V2 staging MySQL replica' >&2; exit 1;
}

channel_count=$(printf "SELECT COUNT(*) FROM performance_schema.replication_connection_configuration WHERE CHANNEL_NAME='%s';\n" "$channel" | mysql_root)
[[ "$channel_count" == 0 ]] || {
  echo 'Replication channel already exists; refusing to reset the destination' >&2; exit 1;
}

aws s3 cp "s3://${BACKUP_BUCKET}/${SNAPSHOT_READY_KEY}" "$temp_dir/ready" \
  --region "$region" --only-show-errors
marker_value() { awk -F= -v name="$1" '$1 == name { print $2 }' "$temp_dir/ready"; }
snapshot_key=$(marker_value snapshot_key)
source_uuid=$(marker_value source_uuid)
source_file=$(marker_value source_file)
source_position=$(marker_value source_position)
expected_sha=$(marker_value sha256)
expected_size=$(marker_value compressed_bytes)

[[ "$snapshot_key" == "${SNAPSHOT_READY_KEY%.ready}.sql.gz" ]] || {
  echo 'The marker does not match the selected snapshot' >&2; exit 1;
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
# backup before resetting yeodam; this step must succeed or nothing is reset.
destination_backup=$(sudo /usr/local/sbin/yeodam-mysql-full-backup)
[[ "$destination_backup" == *'.ready'* ]] || {
  echo 'Destination backup did not produce a ready marker' >&2; exit 1;
}
echo "$destination_backup"

printf 'SET SESSION sql_log_bin=0; DROP DATABASE IF EXISTS yeodam;\n' | mysql_root
{
  printf 'SET SESSION sql_log_bin=0;\n'
  aws s3 cp "s3://${BACKUP_BUCKET}/${snapshot_key}" - \
    --region "$region" --only-show-errors | gzip -dc
} | mysql_root

table_count=$(printf "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='yeodam';\n" | mysql_root)
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
printf "CHANGE REPLICATION FILTER REPLICATE_WILD_DO_TABLE = ('yeodam.%%') FOR CHANNEL '%s';\n" "$channel" | mysql_root
printf "START REPLICA FOR CHANNEL '%s';\n" "$channel" | mysql_root

for _ in $(seq 1 30); do
  status=$(printf "SHOW REPLICA STATUS FOR CHANNEL '%s';\n" "$channel" \
    | sudo docker exec -i "$mysql_container" sh -ec \
        'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot --vertical')
  if grep -Eq '^ *Replica_IO_Running: Yes$' <<<"$status" \
      && grep -Eq '^ *Replica_SQL_Running: Yes$' <<<"$status" \
      && grep -Eq '^ *Last_IO_Errno: 0$' <<<"$status" \
      && grep -Eq '^ *Last_SQL_Errno: 0$' <<<"$status"; then
    printf '%s\n' "$status" | grep -E '^ *(Channel_Name|Source_Host|Source_Log_File|Read_Source_Log_Pos|Replica_IO_Running|Replica_SQL_Running|Seconds_Behind_Source|Last_IO_Errno|Last_SQL_Errno|Source_SSL_CA_File):'
    echo "Imported $table_count source tables; replication channel $channel is running."
    exit 0
  fi
  sleep 2
done

echo 'Replication did not become healthy; the destination backup remains available' >&2
printf '%s\n' "$status" | grep -E '^ *(Channel_Name|Replica_IO_Running|Replica_SQL_Running|Last_IO_Errno|Last_IO_Error|Last_SQL_Errno|Last_SQL_Error):' >&2 || true
exit 1
