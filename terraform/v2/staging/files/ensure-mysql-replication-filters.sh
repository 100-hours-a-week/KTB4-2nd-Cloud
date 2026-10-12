#!/usr/bin/env bash
set -Eeuo pipefail
set +x

unit=/etc/systemd/system/yeodam-mysql.service
needle='--relay-log-recovery=ON '
filters='--replicate-rewrite-db=v1_to_v2:yeodam_v1_source->yeodam --replicate-wild-do-table=v1_to_v2:yeodam.% '

[[ -f "$unit" ]] || { echo 'MySQL systemd unit is missing' >&2; exit 1; }
if grep -Fq -- "$filters" "$unit"; then
  echo 'Persistent V1 application schema filters are already configured.'
  exit 0
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
sed "s@${needle}@${needle}${filters}@" "$backup" >"$unit"
systemctl daemon-reload
systemctl restart yeodam-mysql.service

for _ in $(seq 1 60); do
  if printf 'SELECT 1;\n' | docker exec -i yeodam-v2-mysql sh -ec \
      'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot --batch --skip-column-names' \
      >/dev/null 2>&1; then
    echo 'MySQL is accepting SQL with persistent replication filters.'
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
