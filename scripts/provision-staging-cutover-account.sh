#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077
[[ $EUID -eq 0 && ${SOURCE_HOST:-} =~ ^10\.30\.[0-9]+\.[0-9]+$ ]] || exit 1
prefix=/yeodam/v2/staging/migration
mysql_root() {
  docker exec -i yeodam-v2-mysql sh -ec \
    'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot --batch --skip-column-names'
}
[[ $(printf 'SELECT @@server_id;\n' | mysql_root) == 2 ]] || exit 1
existing=$(printf "SELECT COUNT(*) FROM mysql.user WHERE user='yeodam_cutover' AND host='%s';\n" "$SOURCE_HOST" | mysql_root)
if [[ $existing == 0 ]]; then
  password=$(openssl rand -hex 32)
  # CREATE USER contains a secret: keep it on stdin and outside command output.
  printf "CREATE USER 'yeodam_cutover'@'%s' IDENTIFIED BY '%s' REQUIRE SSL;\n" "$SOURCE_HOST" "$password" | mysql_root >/dev/null
  printf '%s' "$password" > /run/yeodam-cutover-password
  trap 'rm -f /run/yeodam-cutover-password; unset password' EXIT
  aws ssm put-parameter --region ap-northeast-2 --name "$prefix/MYSQL_TARGET_PASSWORD" \
    --type SecureString --value file:///run/yeodam-cutover-password --overwrite >/dev/null
else
  # Do not silently rotate a published credential. An interrupted first setup
  # before publication needs explicit account repair.
  aws ssm get-parameter --region ap-northeast-2 --name "$prefix/MYSQL_TARGET_PASSWORD" \
    --query Parameter.Name --output text >/dev/null
fi
printf "GRANT SYSTEM_VARIABLES_ADMIN,REPLICATION_SLAVE_ADMIN,REPLICATION CLIENT,PROCESS ON *.* TO 'yeodam_cutover'@'%s'; GRANT SELECT ON yeodam_v1_source.* TO 'yeodam_cutover'@'%s';\n" \
  "$SOURCE_HOST" "$SOURCE_HOST" | mysql_root >/dev/null
docker exec yeodam-v2-mysql cat /var/lib/mysql/ca.pem > /run/yeodam-cutover-ca.pem
aws ssm put-parameter --region ap-northeast-2 --name "$prefix/TARGET_CA_PEM" \
  --type String --value file:///run/yeodam-cutover-ca.pem --overwrite >/dev/null
rm -f /run/yeodam-cutover-ca.pem
echo 'Staging cutover account and CA published; no DB route or read-only setting changed.'
