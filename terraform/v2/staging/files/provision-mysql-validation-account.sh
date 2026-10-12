#!/bin/sh
set +x
set -eu

parameter_name=/yeodam/v2/staging/app/MYSQL_VALIDATION_PASSWORD
mysql_container=yeodam-v2-mysql
app_user=yeodam_validation
app_host='10.30.10.%'

password=$(aws ssm get-parameter \
  --region ap-northeast-2 \
  --name "$parameter_name" \
  --with-decryption \
  --query Parameter.Value \
  --output text)

case "$password" in
  *[!0-9a-fA-F]*|'') echo 'MYSQL_VALIDATION_PASSWORD must be a hex string' >&2; exit 1 ;;
esac
[ "${#password}" -eq 64 ] || { echo 'MYSQL_VALIDATION_PASSWORD must be 64 hex characters' >&2; exit 1; }

printf "CREATE DATABASE IF NOT EXISTS yeodam_validation CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;\nCREATE USER IF NOT EXISTS '%s'@'%s' IDENTIFIED BY '%s';\nALTER USER '%s'@'%s' IDENTIFIED BY '%s';\nGRANT ALL PRIVILEGES ON yeodam_validation.* TO '%s'@'%s';\n" \
  "$app_user" "$app_host" "$password" \
  "$app_user" "$app_host" "$password" \
  "$app_user" "$app_host" | \
  sudo docker exec -i "$mysql_container" sh -ec \
    'export MYSQL_PWD=$(cat /run/secrets/mysql-root-password); exec mysql -uroot'
unset password

sudo docker exec "$mysql_container" sh -ec \
  "export MYSQL_PWD=\$(cat /run/secrets/mysql-root-password); exec mysql -uroot -NBe \"SELECT COUNT(*) FROM mysql.user WHERE User='${app_user}' AND Host='${app_host}'\"" | \
  grep -qx 1

echo 'Staging MySQL validation schema and account are provisioned.'
