#!/usr/bin/env bash
set -Eeuo pipefail
set +x

[[ $EUID -eq 0 ]] || { echo 'root required' >&2; exit 1; }
[[ -n ${CDC_LIVE_DDL_B64:-} ]] || { echo 'DDL missing' >&2; exit 1; }

mysql_root() {
  docker exec -i yeodam-mysql-cdc-test sh -ec \
    'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot --batch --skip-column-names'
}

[[ $(printf 'SELECT @@server_id;\n' | mysql_root) == 300 ]] || {
  echo 'Not the dedicated B1 destination' >&2; exit 1;
}

# The pilot schema and its offsets are evidence. Never clear or reuse them.
pilot_tables=$(printf "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='yeodam_cdc';\n" | mysql_root)
[[ $pilot_tables == 3 ]] || {
  echo "Unexpected pilot schema: $pilot_tables table(s)" >&2; exit 1;
}
full309_tables=$(printf "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='yeodam_cdc_full309';\n" | mysql_root)
[[ $full309_tables == 3 ]] || {
  echo "Unexpected preserved #309 schema: $full309_tables table(s)" >&2; exit 1;
}
full_tables=$(printf "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='yeodam_cdc_live313';\n" | mysql_root)
full_schema=$(printf "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='yeodam_cdc_live313';\n" | mysql_root)
[[ $full_tables == 0 && $full_schema == 0 ]] || {
  echo "Full destination already exists (schema=$full_schema tables=$full_tables); inspect before retry" >&2
  exit 1
}

printf "CREATE DATABASE yeodam_cdc_live313 CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;\n" | mysql_root
printf '%s' "$CDC_LIVE_DDL_B64" | base64 --decode | mysql_root >/dev/null
printf "GRANT SELECT, INSERT, UPDATE, DELETE ON yeodam_cdc_live313.* TO 'yeodam_cdc_sink'@'10.30.10.95';\n" | mysql_root
printf "SELECT table_name FROM information_schema.tables WHERE table_schema='yeodam_cdc_live313' ORDER BY table_name;\n" | mysql_root
printf "SELECT table_name,referenced_table_name FROM information_schema.key_column_usage WHERE table_schema='yeodam_cdc_live313' AND referenced_table_name IS NOT NULL ORDER BY table_name;\n" | mysql_root
