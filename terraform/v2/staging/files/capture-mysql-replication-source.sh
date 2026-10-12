#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

region=ap-northeast-2
[[ "${BACKUP_BUCKET:-}" =~ ^yeodam-v2-staging-mysql-backup-[0-9]{12}-ap-northeast-2$ ]] || {
  echo 'Unexpected staging backup bucket' >&2
  exit 1
}

server_id=$(sudo mysql --protocol=socket -uroot -NBe 'SELECT @@server_id')
server_uuid=$(sudo mysql --protocol=socket -uroot -NBe 'SELECT @@server_uuid')
[[ "$server_id" == 1 && "$server_uuid" =~ ^[0-9a-f-]{36}$ ]] || {
  echo 'This is not the expected V1 staging source' >&2
  exit 1
}
source_schema=yeodam_v1_source
table_count=$(sudo mysql --protocol=socket -uroot -NBe "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='${source_schema}' AND table_type='BASE TABLE'")
[[ "$table_count" =~ ^[0-9]+$ && "$table_count" -gt 0 ]] || {
  echo 'The V1 application schema has no tables' >&2; exit 1;
}

stamp="$(date -u +%Y%m%dT%H%M%SZ)-$(openssl rand -hex 4)"
key="migration/v1-source/${stamp}.sql.gz"
ready_key="migration/v1-source/${stamp}.ready"
temp_dir=$(mktemp -d)
hash_pid=
cleanup() {
  if [[ -n "$hash_pid" ]]; then
    kill "$hash_pid" >/dev/null 2>&1 || true
  fi
  rm -rf "$temp_dir"
}
trap cleanup EXIT

mkfifo "$temp_dir/hash.fifo"
sha256sum <"$temp_dir/hash.fifo" >"$temp_dir/hash.out" &
hash_pid=$!

# --source-data=2 captures coordinates while --single-transaction takes the
# InnoDB snapshot. DDL against the V1 application schema must not run during
# this dump. Omit --databases so the dump can be loaded into destination yeodam.
sudo mysqldump --protocol=socket -uroot "$source_schema" \
  --single-transaction --quick --source-data=2 --set-gtid-purged=OFF \
  --routines --events --triggers \
  | awk -v path="$temp_dir/coordinates" \
      '/^-- CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE=/ { print > path } { print }' \
  | gzip -1 \
  | tee "$temp_dir/hash.fifo" \
  | aws s3 cp - "s3://${BACKUP_BUCKET}/${key}" --region "$region" \
      --sse AES256 --expected-size 128849018880 --only-show-errors
wait "$hash_pid"
hash_pid=

source_file=$(sed -n "s/.*SOURCE_LOG_FILE='\([^']*\)'.*/\1/p" "$temp_dir/coordinates")
source_position=$(sed -n 's/.*SOURCE_LOG_POS=\([0-9]*\).*/\1/p' "$temp_dir/coordinates")
checksum=$(awk '{print $1}' "$temp_dir/hash.out")
[[ "$source_file" =~ ^binlog\.[0-9]+$ && "$source_position" =~ ^[0-9]+$ && "$checksum" =~ ^[0-9a-f]{64}$ ]] || {
  echo 'Snapshot coordinates or checksum are invalid' >&2
  exit 1
}

size=$(aws s3api head-object --bucket "$BACKUP_BUCKET" --key "$key" \
  --region "$region" --query ContentLength --output text)
[[ "$size" =~ ^[0-9]+$ && "$size" -gt 0 ]] || {
  echo 'The uploaded snapshot is empty' >&2
  exit 1
}

printf 'snapshot_key=%s\nsource_schema=%s\nsource_uuid=%s\nsource_file=%s\nsource_position=%s\nsha256=%s\ncompressed_bytes=%s\ncreated_at_utc=%s\n' \
  "$key" "$source_schema" "$server_uuid" "$source_file" "$source_position" \
  "$checksum" "$size" "$stamp" >"$temp_dir/ready"
aws s3 cp "$temp_dir/ready" "s3://${BACKUP_BUCKET}/${ready_key}" \
  --region "$region" --sse AES256 --only-show-errors

echo "Source snapshot ready: s3://${BACKUP_BUCKET}/${ready_key}"
echo "Source UUID: $server_uuid; start position: ${source_file}:${source_position}; compressed bytes: $size"
