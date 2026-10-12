#!/usr/bin/env bash
set -Eeuo pipefail
set +x

unit=/etc/systemd/system/yeodam-mysql.service
expected='--log-bin=mysql-bin '
addition='--log-bin=mysql-bin --relay-log=yeodam-relay-bin --relay-log-index=yeodam-relay-bin.index --relay-log-recovery=ON '

[[ -f "$unit" ]] || { echo 'MySQL systemd unit is missing' >&2; exit 1; }
if grep -Fq -- '--relay-log=' "$unit"; then
  if grep -Fq -- "$addition" "$unit"; then
    echo 'Stable relay log is already configured.'
    exit 0
  fi
  echo 'A different relay log setting exists; refusing to modify it' >&2
  exit 1
fi
[[ $(grep -Foc -- "$expected" "$unit") == 1 ]] || {
  echo 'Unexpected MySQL systemd unit; refusing to modify it' >&2
  exit 1
}

backup=$(mktemp "${unit}.before-relay.XXXXXXXX")
cp "$unit" "$backup"
sed "s/${expected}/${addition}/" "$backup" >"$unit"
systemctl daemon-reload
systemctl restart yeodam-mysql.service

for _ in $(seq 1 60); do
  if printf 'SELECT 1;\n' | docker exec -i yeodam-v2-mysql sh -ec \
      'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot --batch --skip-column-names' \
      >/dev/null 2>&1; then
    echo 'MySQL is accepting SQL after the stable relay log change.'
    echo "Previous unit: $backup"
    exit 0
  fi
  sleep 2
done

echo "MySQL did not accept SQL after restart; previous unit: $backup" >&2
exit 1
