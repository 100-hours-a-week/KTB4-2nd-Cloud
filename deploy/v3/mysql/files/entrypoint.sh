#!/bin/bash
source /runtime/common.sh
[[ ${POD_NAME:-} == mysql-0 || ${POD_NAME:-} == mysql-1 ]] || fail 'unexpected member name'
id=$((100 + ${POD_NAME##*-}))
mkdir -p /var/lib/mysql/data /var/lib/mysql/scratch
if [[ ! -d /var/lib/mysql/data/mysql ]]; then
  [[ -z $(ls -A /var/lib/mysql/data) ]] || fail 'nonempty uninitialized data directory'
  mysqld --no-defaults --initialize-insecure --datadir=/var/lib/mysql/data \
    --innodb-buffer-pool-size=256M --innodb-redo-log-capacity=100M
fi
if [[ ! -f /var/lib/mysql/.bootstrap-complete ]]; then
  # Any prior partial initialization is quarantined, never reinitialized/reset.
  [[ ! -f /var/lib/mysql/.bootstrap-started ]] || fail 'partial account initialization requires review'
  touch /var/lib/mysql/.bootstrap-started
  mysqld --no-defaults --datadir=/var/lib/mysql/data --socket=/tmp/mysql.sock \
    --pid-file=/tmp/mysql.pid --skip-networking --skip-log-bin \
    --innodb-buffer-pool-size=256M --innodb-redo-log-capacity=100M &
  init_pid=$!
  trap 'kill -TERM "$init_pid" 2>/dev/null || true; wait "$init_pid" 2>/dev/null || true' EXIT
  for ((i=0; i<120; i++)); do
    mysqladmin --no-defaults --socket=/tmp/mysql.sock ping --silent >/dev/null 2>&1 && break
    kill -0 "$init_pid" 2>/dev/null || fail 'initial server exited'
    sleep 1
  done
  root=$(password root); admin=$(password admin); app=$(password app)
  repl=$(password replication); health=$(password health)
  # Local socket only; no binlogs/GTIDs for member-local account creation.
  if ! mysql --no-defaults --protocol=SOCKET --socket=/tmp/mysql.sock -uroot \
      > /dev/null 2>/tmp/initial-account-error <<SQL
SET SESSION sql_log_bin=0;
CREATE DATABASE yeodam CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER 'yeodam'@'%' IDENTIFIED BY '$app' REQUIRE SSL;
GRANT ALL PRIVILEGES ON yeodam.* TO 'yeodam'@'%';
CREATE USER 'cloud_replication'@'%' IDENTIFIED BY '$repl' REQUIRE SSL;
GRANT REPLICATION SLAVE ON *.* TO 'cloud_replication'@'%';
CREATE USER 'cloud_admin'@'%' IDENTIFIED BY '$admin' REQUIRE SSL;
GRANT SYSTEM_VARIABLES_ADMIN, REPLICATION_SLAVE_ADMIN ON *.* TO 'cloud_admin'@'%';
GRANT REPLICATION CLIENT ON *.* TO 'cloud_admin'@'%';
GRANT SELECT ON performance_schema.* TO 'cloud_admin'@'%';
CREATE USER 'cloud_health'@'localhost' IDENTIFIED BY '$health';
ALTER USER 'root'@'localhost' IDENTIFIED BY '$root';
SQL
  then
    rm -f /tmp/initial-account-error
    fail 'initial account setup rejected; no automatic reset'
  fi
  rm -f /tmp/initial-account-error
  unset root admin app repl health
  client_file root local /tmp/root.cnf root
  mysqladmin --defaults-extra-file=/tmp/root.cnf shutdown
  wait "$init_pid"
  rm -f /tmp/root.cnf
  touch /var/lib/mysql/.bootstrap-complete
  trap - EXIT
fi
# Neither ordinal is automatically writable after restart/promotion.
source_enabled=OFF
[[ $POD_NAME == $PRIMARY_MEMBER ]] && source_enabled=ON
exec mysqld --defaults-file=/runtime/my.cnf --server-id="$id" \
  --rpl-semi-sync-source-enabled="$source_enabled"
