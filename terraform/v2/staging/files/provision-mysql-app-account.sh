#!/bin/sh
set +x
set -eu

parameter_name=/yeodam/v2/staging/app/MYSQL_PASSWORD
mysql_container=yeodam-v2-mysql
app_user=yeodam_app
# Current private App subnet is 10.30.10.0/24. Update this with the subnet.
app_host='10.30.10.%'

password=$(aws ssm get-parameter \
  --region ap-northeast-2 \
  --name "$parameter_name" \
  --with-decryption \
  --query Parameter.Value \
  --output text)

# A generated hex password is safe to quote in SQL and is never sent to SSM Run Command.
case "$password" in
  *[!0-9a-fA-F]*|'') echo 'MYSQL_PASSWORD must be a hex string' >&2; exit 1 ;;
esac
[ "${#password}" -eq 64 ] || { echo 'MYSQL_PASSWORD must be 64 hex characters' >&2; exit 1; }

printf "CREATE USER IF NOT EXISTS '%s'@'%s' IDENTIFIED BY '%s';\nALTER USER '%s'@'%s' IDENTIFIED BY '%s';\nGRANT ALL PRIVILEGES ON yeodam.* TO '%s'@'%s';\n" \
  "$app_user" "$app_host" "$password" \
  "$app_user" "$app_host" "$password" \
  "$app_user" "$app_host" | \
  sudo docker exec -i "$mysql_container" sh -ec \
    'export MYSQL_PWD=$(cat /run/secrets/mysql-root-password); exec mysql -uroot yeodam'
unset password

sudo docker exec "$mysql_container" sh -ec \
  "export MYSQL_PWD=\$(cat /run/secrets/mysql-root-password); exec mysql -uroot -NBe \"SELECT COUNT(*) FROM mysql.user WHERE User='${app_user}' AND Host='${app_host}'\"" | \
  grep -qx 1

echo 'Staging MySQL application account is provisioned.'
