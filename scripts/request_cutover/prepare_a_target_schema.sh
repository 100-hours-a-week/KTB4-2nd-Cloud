#!/usr/bin/env bash
# Run only on the V1 staging Source after the A controller's read-only check.
# The Target is re-fenced even if Flyway fails. No data or journal is reset.
set -Eeuo pipefail
umask 077

run_id='d7dae9c5-5808-4a63-9a35-96cec0659660'
target='10.30.10.28'
source_uuid='10b57eb5-c31c-11f1-9703-02fffcc7ad39'
target_uuid='64c29e6a-c243-11f1-9cc6-1ed3163f5883'
sql_dir="/var/lib/yeodam/a-rehearsal-schema/${run_id}/sql"
expected_bundle_sha='fa373e917ac1e58b375d9644886b3647e1f5bcff604ff4944a1bd0e3dc109072'

[[ $EUID -eq 0 && ${1:-} == "$run_id" && ${2:-} == "$run_id" ]] || {
  echo 'Root and matching Run ID/confirmation required' >&2
  exit 1
}
[[ $(hostname -I) == *'10.30.0.174'* ]] || { echo 'Wrong Source host' >&2; exit 1; }
[[ -d "$sql_dir" ]] || { echo 'Missing reviewed migration files' >&2; exit 1; }
echo "$expected_bundle_sha  $(dirname "$sql_dir")/bundle.tar.gz" | sha256sum -c - >/dev/null

lock='/var/lib/yeodam/migration-cutover/lock'
exec 9>"$lock"
flock -n 9 || { echo 'A DB controller is active' >&2; exit 1; }
[[ ! -e /var/lib/yeodam/migration-cutover/active.json ]] || {
  echo 'Existing A DB journal requires inspection' >&2
  exit 1
}

private=$(mktemp -d /run/a358-schema.XXXXXX)
unfenced=0
target_sql() {
  MYSQL_PWD="$(cat "$private/target-password")" mysql --batch --skip-column-names \
    --connect-timeout=2 --protocol=tcp -h"$target" -uyeodam_cutover \
    --ssl-mode=VERIFY_CA --ssl-ca="$private/target-ca.pem" -e "$1"
}
cleanup() {
  status=$?
  trap - EXIT
  if [[ $unfenced -eq 1 ]]; then
    if ! target_sql 'SET PERSIST super_read_only=ON; SET PERSIST read_only=ON;'; then
      echo 'CRITICAL: Target re-fence failed; inspect Writer state before any rehearsal' >&2
      status=1
    fi
  fi
  rm -rf -- "$private"
  exit "$status"
}
trap cleanup EXIT

aws ssm get-parameter --region ap-northeast-2 --name /yeodam/v2/staging/migration/MYSQL_TARGET_PASSWORD \
  --with-decryption --query Parameter.Value --output text > "$private/target-password"
aws ssm get-parameter --region ap-northeast-2 --name /yeodam/v2/staging/migration/TARGET_CA_PEM \
  --with-decryption --query Parameter.Value --output text > "$private/target-ca.pem"
aws ssm get-parameter --region ap-northeast-2 --name /yeodam/v2/staging/v1-source/app/MYSQL_PASSWORD \
  --with-decryption --query Parameter.Value --output text > "$private/app-password"

[[ $(mysql --batch --skip-column-names -uroot --protocol=socket -e 'SELECT @@server_uuid;') == "$source_uuid" ]] || {
  echo 'Wrong Source UUID' >&2; exit 1;
}
[[ $(target_sql 'SELECT @@server_uuid,@@read_only,@@super_read_only;') == "$target_uuid"$'\t1\t1' ]] || {
  echo 'Wrong Target UUID or read-only state' >&2; exit 1;
}
[[ $(target_sql 'SELECT MAX(CAST(version AS UNSIGNED)) FROM yeodam_v1_source.flyway_schema_history;') == 13 ]] || {
  echo 'Target Flyway baseline changed' >&2; exit 1;
}

flyway() {
  docker run --rm --network host --user 0:0 \
    -v "$sql_dir:/flyway/sql:ro" \
    -v "$private/app-password:/run/secrets/mysql-password:ro" \
    --entrypoint /bin/sh flyway/flyway:13.6.0 -c \
    'export FLYWAY_PASSWORD="$(cat /run/secrets/mysql-password)" FLYWAY_USER=yeodam_app FLYWAY_URL="jdbc:mysql://10.30.10.28:3306/yeodam_v1_source?sslMode=REQUIRED"; exec /flyway/flyway -locations=filesystem:/flyway/sql "$@"' \
    -- "$@"
}

flyway -ignoreMigrationPatterns='*:pending' validate
target_sql 'SET SESSION lock_wait_timeout=2; SET PERSIST super_read_only=OFF; SET PERSIST read_only=OFF;'
unfenced=1
[[ $(target_sql 'SELECT @@server_uuid,@@read_only,@@super_read_only;') == "$target_uuid"$'\t0\t0' ]] || {
  echo 'Target did not enter controlled schema window' >&2; exit 1;
}
flyway migrate
target_sql 'SET PERSIST super_read_only=ON; SET PERSIST read_only=ON;'
[[ $(target_sql 'SELECT @@server_uuid,@@read_only,@@super_read_only;') == "$target_uuid"$'\t1\t1' ]] || {
  echo 'Target re-fence verification failed' >&2; exit 1;
}
unfenced=0
[[ $(target_sql 'SELECT MAX(CAST(version AS UNSIGNED)) FROM yeodam_v1_source.flyway_schema_history;') == 25 ]] || {
  echo 'Flyway did not reach V25' >&2; exit 1;
}
flyway validate
echo 'A_TARGET_SCHEMA_V25_VALIDATED'
