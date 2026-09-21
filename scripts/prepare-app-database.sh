#!/usr/bin/env bash

set -Eeuo pipefail

readonly DATABASE_NAME="yeodam"
readonly DATABASE_USER="yeodam_app"
readonly DATABASE_HOST_PATTERN="172.28.0.%"
readonly CREDENTIAL_DIR="/etc/yeodam"
readonly CREDENTIAL_FILE="${CREDENTIAL_DIR}/mysql.env"

database_password=""

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  echo
  echo "==> $*"
}

if [[ "${EUID}" -ne 0 ]]; then
  fail "root 권한으로 실행해야 합니다."
fi

command -v mysql >/dev/null 2>&1 ||
  fail "mysql Client를 찾을 수 없습니다."

command -v openssl >/dev/null 2>&1 ||
  fail "openssl 명령을 찾을 수 없습니다."

systemctl is-active --quiet mysql ||
  fail "MySQL Service가 실행 중이 아닙니다."

install -d \
  --owner=root \
  --group=root \
  --mode=700 \
  "${CREDENTIAL_DIR}"

if [[ -f "${CREDENTIAL_FILE}" ]]; then
  database_password="$(
    sed -n 's/^MYSQL_PASSWORD=//p' "${CREDENTIAL_FILE}"
  )"

  [[ "${database_password}" =~ ^[0-9a-f]{64}$ ]] ||
    fail "기존 ${CREDENTIAL_FILE}의 MYSQL_PASSWORD 형식이 올바르지 않습니다."

  chmod 600 "${CREDENTIAL_FILE}"
  chown root:root "${CREDENTIAL_FILE}"
else
  database_password="$(openssl rand -hex 32)"

  temporary_file="$(mktemp)"
  trap 'rm -f "${temporary_file}"' EXIT

  {
    printf 'MYSQL_URL=jdbc:mysql://host.docker.internal:3306/%s\n' "${DATABASE_NAME}"
    printf 'MYSQL_USERNAME=%s\n' "${DATABASE_USER}"
    printf 'MYSQL_PASSWORD=%s\n' "${database_password}"
  } >"${temporary_file}"

  install \
    --owner=root \
    --group=root \
    --mode=600 \
    "${temporary_file}" \
    "${CREDENTIAL_FILE}"

  rm -f "${temporary_file}"
  trap - EXIT
fi

log "Application Database와 Backend 사용자 구성"

mysql --protocol=socket --user=root <<SQL
CREATE DATABASE IF NOT EXISTS \`${DATABASE_NAME}\`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

CREATE USER IF NOT EXISTS
  '${DATABASE_USER}'@'${DATABASE_HOST_PATTERN}'
  IDENTIFIED BY '${database_password}';

ALTER USER
  '${DATABASE_USER}'@'${DATABASE_HOST_PATTERN}'
  IDENTIFIED BY '${database_password}';

GRANT ALL PRIVILEGES
  ON \`${DATABASE_NAME}\`.*
  TO '${DATABASE_USER}'@'${DATABASE_HOST_PATTERN}';
SQL

log "구성 확인"

mysql \
  --protocol=socket \
  --user=root \
  --batch \
  --skip-column-names \
  --execute="
    SELECT SCHEMA_NAME, DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME
    FROM information_schema.SCHEMATA
    WHERE SCHEMA_NAME = '${DATABASE_NAME}';

    SELECT User, Host
    FROM mysql.user
    WHERE User = '${DATABASE_USER}';
  "

stat \
  --format='%U:%G %a %n' \
  "${CREDENTIAL_FILE}"

echo
echo "Application Database 준비 완료"
echo "Credential 값은 출력하지 않았습니다."
