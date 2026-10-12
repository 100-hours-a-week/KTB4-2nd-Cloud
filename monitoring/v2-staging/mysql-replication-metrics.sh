#!/usr/bin/env bash
set -Eeuo pipefail
set +x

role=${1:-}
[[ "$role" == source || "$role" == replica ]] || {
  echo 'Usage: mysql-replication-metrics.sh source|replica' >&2
  exit 2
}

directory=${METRICS_DIRECTORY:-/var/lib/yeodam/mysql-metrics}
install -d -m 0755 "$directory"
output="$directory/replication.prom"
temporary=$(mktemp "$directory/.replication.XXXXXXXX")
trap 'rm -f "$temporary"' EXIT
sampled_at=$(date +%s)

failure() {
  printf 'yeodam_mysql_%s_probe_success 0\n' "$role" >"$temporary"
  printf 'yeodam_mysql_%s_sample_timestamp_seconds %s\n' "$role" "$sampled_at" >>"$temporary"
  chmod 0644 "$temporary"
  mv "$temporary" "$output"
  echo "MySQL $role metric probe failed" >&2
  exit 1
}

file_sequence() {
  [[ "$1" =~ ^binlog\.([0-9]+)$ ]] || return 1
  printf '%d\n' "$((10#${BASH_REMATCH[1]}))"
}

if [[ "$role" == source ]]; then
  result=$(mysql --protocol=socket -uroot --batch --skip-column-names \
    --execute='SHOW BINARY LOG STATUS' 2>/dev/null) || failure
  read -r file position _ <<<"$result"
  sequence=$(file_sequence "${file:-}") || failure
  [[ "${position:-}" =~ ^[0-9]+$ ]] || failure
  cat >"$temporary" <<EOF
yeodam_mysql_source_probe_success 1
yeodam_mysql_source_sample_timestamp_seconds $sampled_at
yeodam_mysql_source_binlog_file_sequence $sequence
yeodam_mysql_source_binlog_position $position
EOF
else
  result=$(printf "SHOW REPLICA STATUS FOR CHANNEL 'v1_to_v2';\n" \
    | docker exec -i yeodam-v2-mysql sh -ec \
      'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot --vertical' \
      2>/dev/null) || failure
  field() {
    awk -F': ' -v wanted="$1" '$1 ~ "^[[:space:]]*" wanted "$" { print $2; exit }' <<<"$result"
  }
  io=$(field Replica_IO_Running)
  sql=$(field Replica_SQL_Running)
  io_errno=$(field Last_IO_Errno)
  sql_errno=$(field Last_SQL_Errno)
  read_file=$(field Source_Log_File)
  read_position=$(field Read_Source_Log_Pos)
  exec_file=$(field Relay_Source_Log_File)
  exec_position=$(field Exec_Source_Log_Pos)
  lag=$(field Seconds_Behind_Source)
  [[ "$io" == Yes || "$io" == No ]] || failure
  [[ "$sql" == Yes || "$sql" == No ]] || failure
  [[ "$io_errno" =~ ^[0-9]+$ && "$sql_errno" =~ ^[0-9]+$ ]] || failure
  [[ "$read_position" =~ ^[0-9]+$ && "$exec_position" =~ ^[0-9]+$ ]] || failure
  read_sequence=$(file_sequence "$read_file") || failure
  exec_sequence=$(file_sequence "$exec_file") || failure
  [[ "$lag" == NULL || "$lag" =~ ^[0-9]+$ ]] || failure
  [[ "$io" == Yes ]] && io=1 || io=0
  [[ "$sql" == Yes ]] && sql=1 || sql=0
  cat >"$temporary" <<EOF
yeodam_mysql_replica_probe_success 1
yeodam_mysql_replica_sample_timestamp_seconds $sampled_at
yeodam_mysql_replica_io_running $io
yeodam_mysql_replica_sql_running $sql
yeodam_mysql_replica_last_io_errno $io_errno
yeodam_mysql_replica_last_sql_errno $sql_errno
yeodam_mysql_replica_read_file_sequence $read_sequence
yeodam_mysql_replica_read_position $read_position
yeodam_mysql_replica_exec_file_sequence $exec_sequence
yeodam_mysql_replica_exec_position $exec_position
EOF
  if [[ "$lag" != NULL ]]; then
    printf 'yeodam_mysql_replica_lag_seconds %s\n' "$lag" >>"$temporary"
  fi
fi

chmod 0644 "$temporary"
mv "$temporary" "$output"
