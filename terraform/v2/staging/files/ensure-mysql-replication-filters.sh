#!/usr/bin/env bash
set -Eeuo pipefail
set +x

unit=/etc/systemd/system/yeodam-mysql.service
container=yeodam-v2-mysql
channel=v1_to_v2
needle='--relay-log-recovery=ON '
legacy='--replicate-rewrite-db=v1_to_v2:yeodam_v1_source->yeodam --replicate-wild-do-table=v1_to_v2:yeodam.% '
desired='--replicate-wild-do-table=v1_to_v2:yeodam_v1_source.% '

mysql_root() {
  sudo docker exec -i "$container" sh -ec \
    'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot --batch --skip-column-names'
}

[[ -f "$unit" ]] || { echo 'MySQL systemd unit is missing' >&2; exit 1; }
if grep -Fq -- "$desired" "$unit"; then
  if grep -Fq -- '--replicate-rewrite-db=' "$unit"; then
    echo 'Unexpected schema rewrite remains in the MySQL unit' >&2; exit 1
  fi
  echo 'Persistent V1-compatible schema filter is already configured.'
  exit 0
fi

if grep -Fq -- "$legacy" "$unit"; then
  [[ "${CONFIRM_FILTER_MIGRATION:-}" == MIGRATE_V1_COMPAT_FILTER_AFTER_BACKUP ]] || {
    echo 'Explicit legacy filter migration confirmation is required' >&2; exit 1;
  }
  [[ "${SOURCE_HOST:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo 'Expected V1 source host is required' >&2; exit 1;
  }
  channel_count=$(printf "SELECT COUNT(*) FROM performance_schema.replication_connection_configuration WHERE CHANNEL_NAME='%s';\n" "$channel" | mysql_root)
  [[ "$channel_count" == 1 ]] || { echo 'Expected replication channel is missing' >&2; exit 1; }
  configured_source=$(printf "SELECT HOST FROM performance_schema.replication_connection_configuration WHERE CHANNEL_NAME='%s';\n" "$channel" | mysql_root)
  [[ "$configured_source" == "$SOURCE_HOST" ]] || { echo 'Replication channel has an unexpected source' >&2; exit 1; }

  # Back up before removing the old channel. The old yeodam schema is kept.
  backup=$(sudo /usr/local/sbin/yeodam-mysql-full-backup)
  [[ "$backup" == *'.ready'* ]] || { echo 'Full backup did not produce a ready marker' >&2; exit 1; }
  echo "$backup"
  printf "STOP REPLICA FOR CHANNEL '%s'; RESET REPLICA ALL FOR CHANNEL '%s';\n" "$channel" "$channel" | mysql_root
  replacement=$(mktemp "${unit}.v1-compat.XXXXXXXX")
  cp "$unit" "${replacement}.before"
  sed "s@${legacy}@${desired}@" "${replacement}.before" >"$replacement"
  if cmp -s "$unit" "$replacement"; then
    echo 'Legacy filter replacement changed nothing' >&2; exit 1;
  fi
  install -m 0644 "$replacement" "$unit"
  systemctl daemon-reload
  if ! systemctl restart yeodam-mysql.service; then
    install -m 0644 "${replacement}.before" "$unit"
    systemctl daemon-reload
    systemctl restart yeodam-mysql.service || true
    rm -f "$replacement"
    echo 'MySQL start failed with the new filter; previous unit restored. The old channel must be reinitialized.' >&2
    exit 1
  fi
  for _ in $(seq 1 60); do
    if printf 'SELECT 1;\n' | mysql_root >/dev/null 2>&1; then
      echo 'MySQL restarted with V1-compatible filter; initialize a fresh snapshot before resuming replication.'
      echo "Previous unit: ${replacement}.before"
      rm -f "$replacement"
      exit 0
    fi
    sleep 2
  done
  install -m 0644 "${replacement}.before" "$unit"
  systemctl daemon-reload
  systemctl restart yeodam-mysql.service || true
  rm -f "$replacement"
  echo 'MySQL failed with the new filter; previous unit restored. The old channel must be reinitialized.' >&2
  exit 1
fi

if grep -Fq -- '--replicate-rewrite-db=' "$unit" || grep -Fq -- '--replicate-wild-do-table=' "$unit"; then
  echo 'Different replication filters exist; refusing to replace them' >&2
  exit 1
fi
[[ $(grep -Foc -- "$needle" "$unit") == 1 ]] || {
  echo 'Unexpected MySQL systemd unit; refusing to modify it' >&2
  exit 1
}

backup=$(mktemp "${unit}.before-filters.XXXXXXXX")
cp "$unit" "$backup"
sed "s@${needle}@${needle}${desired}@" "$backup" >"$unit"
systemctl daemon-reload
if ! systemctl restart yeodam-mysql.service; then
  cp "$backup" "$unit"
  systemctl daemon-reload
  systemctl restart yeodam-mysql.service || true
  echo "MySQL start failed with the new filter; restored previous unit: $backup" >&2
  exit 1
fi

for _ in $(seq 1 60); do
  if printf 'SELECT 1;\n' | mysql_root >/dev/null 2>&1; then
    echo 'MySQL is accepting SQL with persistent V1-compatible filters.'
    echo "Previous unit: $backup"
    exit 0
  fi
  sleep 2
done

cp "$backup" "$unit"
systemctl daemon-reload
systemctl restart yeodam-mysql.service || true
echo "MySQL failed after changing replication filters; restored previous unit: $backup" >&2
exit 1
