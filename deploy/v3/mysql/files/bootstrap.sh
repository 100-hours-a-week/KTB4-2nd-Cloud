#!/bin/bash
source /runtime/common.sh
member_clients
wait_query "$P" 'SELECT 1;' 1
wait_query "$R" 'SELECT 1;' 1
source_sql="SELECT COUNT(*) FROM performance_schema.replication_connection_configuration;"
[[ $(echo "$source_sql" | query "$P") == 0 ]] || fail 'designated primary has a replication source'
channels=$(echo "$source_sql" | query "$R")
if [[ $channels == 0 ]]; then
  for file in "$P" "$R"; do
    [[ $(echo 'SELECT @@global.read_only AND @@global.super_read_only AND @@global.gtid_executed="";' | query "$file") == 1 ]] || fail 'initial bootstrap requires read-only members with empty GTID history'
  done
  # The hex password never appears in arguments or logs.
  private_sql "$R" <<SQL
CHANGE REPLICATION SOURCE TO
 SOURCE_HOST='$PRIMARY_HOST', SOURCE_PORT=3306,
 SOURCE_USER='cloud_replication', SOURCE_PASSWORD='$(password replication)',
 SOURCE_AUTO_POSITION=1, SOURCE_SSL=1, SOURCE_SSL_CA='/tls/ca.crt',
 SOURCE_SSL_VERIFY_SERVER_CERT=1, SOURCE_CONNECT_RETRY=2
 FOR CHANNEL 'v3';
START REPLICA FOR CHANNEL 'v3';
SQL
  wait_query "$R" "SELECT COUNT(*) FROM performance_schema.replication_connection_status WHERE CHANNEL_NAME='v3' AND SERVICE_STATE='ON';" 1
  wait_query "$P" "SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Rpl_semi_sync_source_clients';" 1
  wait_query "$R" "SELECT COUNT(*) FROM performance_schema.replication_applier_status WHERE CHANNEL_NAME='v3' AND SERVICE_STATE='ON';" 1
  private_sql "$P" <<'SQL'
SET GLOBAL super_read_only=OFF;
SET GLOBAL read_only=OFF;
SQL
else
  [[ $channels == 1 ]] || fail 'unexpected replication channels'
  [[ $(echo "SELECT HOST FROM performance_schema.replication_connection_configuration WHERE CHANNEL_NAME='v3';" | query "$R") == "$PRIMARY_HOST" ]] || fail 'replication source mismatch'
  # Verify-only on retry. Never reopen a restarted/fenced primary automatically.
  [[ $(echo "SELECT NOT @@global.read_only AND NOT @@global.super_read_only;" | query "$P") == 1 ]] || fail "existing channel with read-only primary: explicit maintenance review required"
fi
[[ $(echo "SELECT AUTO_POSITION=1 AND SSL_ALLOWED='YES' AND SSL_VERIFY_SERVER_CERTIFICATE='YES' AND PORT=3306 AND USER='cloud_replication' FROM performance_schema.replication_connection_configuration WHERE CHANNEL_NAME='v3';" | query "$R") == 1 ]] || fail 'replication GTID/TLS configuration mismatch'
wait_query "$P" 'SELECT NOT @@global.read_only AND NOT @@global.super_read_only;' 1
wait_query "$R" 'SELECT @@global.read_only AND @@global.super_read_only;' 1
wait_query "$R" "SELECT COUNT(*) FROM performance_schema.replication_applier_status WHERE CHANNEL_NAME='v3' AND SERVICE_STATE='ON';" 1
wait_query "$R" "SELECT COUNT(*) FROM performance_schema.replication_connection_status WHERE CHANNEL_NAME='v3' AND SERVICE_STATE='ON';" 1
wait_query "$P" "SELECT VARIABLE_VALUE FROM performance_schema.global_status WHERE VARIABLE_NAME='Rpl_semi_sync_source_status';" ON
echo 'PASS: initial GTID/TLS/semi-sync topology; one writable primary'
