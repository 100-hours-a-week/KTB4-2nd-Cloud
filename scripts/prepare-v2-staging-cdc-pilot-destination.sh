#!/usr/bin/env bash
set -Eeuo pipefail
set +x
[[ $EUID -eq 0 ]] || exit 1
[[ -n ${CDC_PILOT_DDL_B64:-} ]] || exit 1
mysql_root() {
  docker exec -i yeodam-mysql-cdc-test sh -ec \
    'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot --batch --skip-column-names'
}
[[ $(printf 'SELECT @@server_id;\n' | mysql_root) == 300 ]] || {
  echo 'Not the dedicated B1 destination' >&2; exit 1;
}
table_count=$(printf "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='yeodam_cdc';\n" | mysql_root)
[[ $table_count == 0 ]] || {
  echo "B1 schema already contains $table_count table(s); inspect before retry" >&2; exit 1;
}
printf '%s' "$CDC_PILOT_DDL_B64" | base64 --decode | mysql_root >/dev/null
printf "SELECT table_name FROM information_schema.tables WHERE table_schema='yeodam_cdc' ORDER BY table_name;\n" | mysql_root
printf "SELECT table_name,referenced_table_name FROM information_schema.key_column_usage WHERE table_schema='yeodam_cdc' AND referenced_table_name IS NOT NULL ORDER BY table_name;\n" | mysql_root
