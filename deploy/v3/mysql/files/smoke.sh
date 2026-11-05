#!/bin/bash
source /runtime/common.sh
member_clients
wait_query "$P" 'SELECT NOT @@global.read_only AND NOT @@global.super_read_only;' 1
A=/tmp/app-$$.cnf
client_file yeodam "$PRIMARY_HOST" "$A" app
# Reserved table belongs only to this smoke check. Never touch service tables.
[[ $(echo "SELECT COUNT(*) FROM performance_schema.replication_connection_status WHERE CHANNEL_NAME='v3' AND SERVICE_STATE='ON';" | query "$R") == 1 ]] || fail 'replica not connected'
[[ $(echo "SHOW SESSION STATUS LIKE 'Ssl_cipher';" | query "$A") == Ssl_cipher$'\t'?* ]] || fail 'TLS missing'
echo "CREATE TABLE IF NOT EXISTS yeodam._cloud_replication_check (id INT PRIMARY KEY, marker VARCHAR(64)); INSERT INTO yeodam._cloud_replication_check VALUES (1, 'v3-initial-replication') ON DUPLICATE KEY UPDATE marker='v3-initial-replication';" | query "$A" >/dev/null
gtid=$(echo 'SELECT @@global.gtid_executed;' | query "$P")
[[ $(printf "SELECT WAIT_FOR_EXECUTED_GTID_SET('%s',30);\n" "$gtid" | query "$R") == 0 ]] || fail 'GTID replication timed out'
# Admin is intentionally not granted app schema access. Use app credential for read verification.
client_file yeodam "$REPLICA_HOST" "$A" app
[[ $(echo 'SELECT marker FROM yeodam._cloud_replication_check WHERE id=1;' | query "$A") == v3-initial-replication ]] || fail 'replica data mismatch'
if echo 'INSERT INTO yeodam._cloud_replication_check VALUES (2,"must-not-write");' | query "$A" >/dev/null 2>/dev/null; then
  fail 'replica accepted an app write'
fi
[[ $(echo "SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Rpl_semi_sync_source_yes_tx';" | query "$P") -ge 1 ]] || fail 'semi-sync acknowledgment not observed'
client_file yeodam "$PRIMARY_HOST" "$A" app
echo 'DROP TABLE yeodam._cloud_replication_check;' | query "$A" >/dev/null
gtid=$(echo 'SELECT @@global.gtid_executed;' | query "$P")
[[ $(printf "SELECT WAIT_FOR_EXECUTED_GTID_SET('%s',30);\n" "$gtid" | query "$R") == 0 ]] || fail 'smoke cleanup not replicated'
echo 'PASS: TLS app write, replicated row, GTID catch-up, read-only rejection and cleanup'
