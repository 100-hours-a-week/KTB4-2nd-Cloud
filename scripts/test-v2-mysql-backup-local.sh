#!/usr/bin/env bash
set -Eeuo pipefail

image='mysql:9.7.2@sha256:e2bde46db6563855d7177adb5f0b57b9dc663f5a20927a90f4259d3312068497'
source_name="yeodam-backup-test-source-$$"
target_name="yeodam-backup-test-target-$$"
source_volume="${source_name}-data"
target_volume="${target_name}-data"
work=$(mktemp -d)
trap 'docker rm -f "$source_name" "$target_name" >/dev/null 2>&1 || true; docker volume rm "$source_volume" "$target_volume" >/dev/null 2>&1 || true; rm -rf "$work"' EXIT

docker volume create "$source_volume" >/dev/null
docker volume create "$target_volume" >/dev/null
docker run -d --name "$source_name" -e MYSQL_ROOT_PASSWORD=local-test-only \
  -v "$source_volume:/var/lib/mysql" "$image" --log-bin=mysql-bin --sync-binlog=1 --innodb-flush-log-at-trx-commit=1 >/dev/null
docker run -d --name "$target_name" -e MYSQL_ROOT_PASSWORD=local-test-only \
  -v "$target_volume:/var/lib/mysql" "$image" --log-bin=mysql-bin --sync-binlog=1 --innodb-flush-log-at-trx-commit=1 >/dev/null

for name in "$source_name" "$target_name"; do
  ready=false
  for attempt in $(seq 1 60); do
    if docker exec "$name" sh -ec 'MYSQL_PWD=local-test-only mysql -uroot -NBe "SELECT 1"' >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 2
  done
  "$ready" || { echo "MySQL did not start: $name" >&2; exit 1; }
done

docker exec "$source_name" sh -ec \
  'MYSQL_PWD=local-test-only mysql -uroot -e "CREATE DATABASE yeodam; CREATE TABLE yeodam.backup_probe (id INT PRIMARY KEY, stage VARCHAR(20)); INSERT INTO yeodam.backup_probe VALUES (1, '\''before'\'');"'

docker exec "$source_name" sh -ec \
  'MYSQL_PWD=local-test-only mysqldump -uroot --databases yeodam --single-transaction --quick --source-data=2 --set-gtid-purged=OFF --routines --events --triggers' \
  | awk -v path="$work/coordinates" '/^-- CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE=/ { print > path } { print }' \
  | gzip -1 > "$work/full.sql.gz"
gzip -t "$work/full.sql.gz"
gzip -dc "$work/full.sql.gz" > "$work/full.sql"
source_file=$(sed -n "s/.*SOURCE_LOG_FILE='\([^']*\)'.*/\1/p" "$work/coordinates")
source_pos=$(sed -n 's/.*SOURCE_LOG_POS=\([0-9]*\).*/\1/p' "$work/coordinates")
[[ "$source_file" =~ ^mysql-bin\.[0-9]+$ && "$source_pos" =~ ^[0-9]+$ ]] || {
  echo 'Full backup has no valid binary log coordinate' >&2
  exit 1
}

docker exec "$source_name" sh -ec \
  'MYSQL_PWD=local-test-only mysql -uroot -e "INSERT INTO yeodam.backup_probe VALUES (2, '\''after-1'\''); FLUSH BINARY LOGS; INSERT INTO yeodam.backup_probe VALUES (3, '\''after-2'\''); FLUSH BINARY LOGS;"'

docker exec -i "$target_name" sh -ec 'MYSQL_PWD=local-test-only mysql -uroot --binary-mode' < "$work/full.sql"
before=$(docker exec "$target_name" sh -ec \
  'MYSQL_PWD=local-test-only mysql -uroot -NBe "SELECT COUNT(*) FROM yeodam.backup_probe"')
[[ "$before" == 1 ]] || { echo "Full backup restored $before rows, expected 1" >&2; exit 1; }

next_number=$((10#${source_file##*.} + 1))
next_file=$(printf 'mysql-bin.%06d' "$next_number")
docker exec "$source_name" /usr/libexec/mysqlsh/mysqlbinlog --start-position="$source_pos" "/var/lib/mysql/$source_file" "/var/lib/mysql/$next_file" \
  | docker exec -i "$target_name" sh -ec 'MYSQL_PWD=local-test-only mysql -uroot --binary-mode'
after=$(docker exec "$target_name" sh -ec \
  'MYSQL_PWD=local-test-only mysql -uroot -NBe "SELECT GROUP_CONCAT(stage ORDER BY id) FROM yeodam.backup_probe"')
[[ "$after" == 'before,after-1,after-2' ]] || { echo "Unexpected restored rows: $after" >&2; exit 1; }

echo "PASS: full backup contained one row; replay from ${source_file}:${source_pos} across two log files restored both later rows"
