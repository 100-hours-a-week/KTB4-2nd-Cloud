#!/bin/sh
set -eu

mysql_container=yeodam-v2-mysql
replica_server_id=2

actual=$(sudo docker exec "$mysql_container" sh -ec \
  'export MYSQL_PWD=$(cat /run/secrets/mysql-root-password); exec mysql -uroot -NBe "SELECT @@server_id"')

case "$actual" in
  1|2) ;;
  *) echo "Unexpected MySQL server_id: $actual" >&2; exit 1 ;;
esac

sudo docker exec "$mysql_container" sh -ec \
  "export MYSQL_PWD=\$(cat /run/secrets/mysql-root-password); exec mysql -uroot -e 'SET PERSIST server_id = $replica_server_id'"

sudo docker exec "$mysql_container" sh -ec \
  'export MYSQL_PWD=$(cat /run/secrets/mysql-root-password); exec mysql -uroot -NBe "SELECT @@server_id"' | \
  grep -qx "$replica_server_id"

echo "V2 staging MySQL server_id=$replica_server_id is persisted."
