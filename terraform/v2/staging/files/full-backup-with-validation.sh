#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
source /etc/yeodam/mysql-backup.env

exec 9>/run/lock/yeodam-mysql-full-backup.lock
flock -n 9 || { echo 'A full backup is already running' >&2; exit 1; }

stamp=$(date -u +%Y%m%dT%H%M%SZ)
key="full/${stamp}.sql.gz"
ready_key="full/${stamp}.ready"
coordinates=$(mktemp /run/yeodam-mysql-coordinates.XXXXXX)
ready=$(mktemp /run/yeodam-mysql-ready.XXXXXX)
trap 'rm -f "$coordinates" "$ready"' EXIT

server_uuid=$(docker exec "$MYSQL_CONTAINER" sh -ec \
  'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot -NBe "SELECT @@server_uuid"')
[[ "$server_uuid" =~ ^[0-9a-fA-F-]{36}$ ]] || { echo 'Invalid MySQL server UUID' >&2; exit 1; }

# Keep the former replica schema in backups while it remains on this host.
# The V1-compatible schema is added after the new snapshot is imported.
schemas=(yeodam_validation)
for schema in yeodam yeodam_v1_source; do
  exists=$(docker exec "$MYSQL_CONTAINER" sh -ec \
    "export MYSQL_PWD=\"\$(cat /run/secrets/mysql-root-password)\"; exec mysql -uroot -NBe \"SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='${schema}'\"")
  [[ "$exists" == 0 || "$exists" == 1 ]] || { echo 'Invalid schema lookup result' >&2; exit 1; }
  if [[ "$exists" == 1 ]]; then schemas+=("$schema"); fi
done

# The completion marker is uploaded only if every stage of this pipeline succeeds.
docker exec "$MYSQL_CONTAINER" sh -ec \
  'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysqldump -uroot --databases "$@" --single-transaction --quick --source-data=2 --set-gtid-purged=OFF --routines --events --triggers' \
  sh "${schemas[@]}" \
  | awk -v path="$coordinates" '/^-- CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE=/ { print > path } { print }' \
  | gzip -1 \
  | aws s3 cp - "s3://${BACKUP_BUCKET}/${key}" --region "$AWS_REGION" --sse AES256 --expected-size 128849018880 --only-show-errors

source_file=$(sed -n "s/.*SOURCE_LOG_FILE='\([^']*\)'.*/\1/p" "$coordinates")
source_pos=$(sed -n 's/.*SOURCE_LOG_POS=\([0-9]*\).*/\1/p' "$coordinates")
[[ "$source_file" =~ ^mysql-bin\.[0-9]+$ && "$source_pos" =~ ^[0-9]+$ ]] || {
  echo 'The dump did not contain a valid binary log coordinate' >&2
  exit 1
}

size=$(aws s3api head-object --bucket "$BACKUP_BUCKET" --key "$key" --region "$AWS_REGION" --query ContentLength --output text)
[[ "$size" =~ ^[0-9]+$ && "$size" -gt 0 ]] || { echo 'The S3 backup is empty' >&2; exit 1; }

printf 'backup_key=%s\nserver_uuid=%s\nsource_file=%s\nsource_position=%s\ncompressed_bytes=%s\ncreated_at_utc=%s\n' \
  "$key" "$server_uuid" "$source_file" "$source_pos" "$size" "$stamp" > "$ready"
aws s3 cp "$ready" "s3://${BACKUP_BUCKET}/${ready_key}" --region "$AWS_REGION" --sse AES256 --only-show-errors
echo "Completed MySQL full backup: s3://${BACKUP_BUCKET}/${ready_key}"
