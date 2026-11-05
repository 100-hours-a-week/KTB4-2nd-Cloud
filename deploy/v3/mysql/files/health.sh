#!/bin/bash
source /runtime/common.sh
if [[ ${1:-} == liveness ]]; then
  # Access denied also proves that the local server answered ping.
  exec mysqladmin --no-defaults --protocol=SOCKET --socket=/tmp/mysql.sock ping --silent 2>/dev/null
fi
config=/tmp/health-$$.cnf
trap 'rm -f "$config"' EXIT
client_file cloud_health local "$config" health
[[ $(echo 'SELECT 1;' | query "$config" 2>/dev/null) == 1 ]] || exit 1
if [[ ${1:-} == readiness ]]; then
  expected=$'1\t1'
  [[ $POD_NAME == $PRIMARY_MEMBER ]] && expected=$'0\t0'
  [[ $(echo 'SELECT @@global.read_only, @@global.super_read_only;' | query "$config" 2>/dev/null) == "$expected" ]]
else
  [[ ${1:-} == startup ]]
fi
