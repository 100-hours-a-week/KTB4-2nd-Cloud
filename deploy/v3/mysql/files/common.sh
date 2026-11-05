#!/bin/bash
set -Eeuo pipefail
umask 077
fail() { echo "MySQL check failed: $*" >&2; exit 1; }
password() {
  local value
  value=$(cat "/credentials/$1-password")
  local length=48
  [[ $1 == replication ]] && length=32
  [[ $value =~ ^[a-f0-9]+$ && ${#value} == "$length" ]] || fail 'invalid bootstrap credential format'
  printf '%s' "$value"
}
client_file() {
  local user=$1 host=$2 path=$3 secret=$4
  {
    printf '[client]\nuser=%s\npassword=%s\nconnect-timeout=3\n' "$user" "$(password "$secret")"
    if [[ $host == local ]]; then
      printf 'protocol=SOCKET\nsocket=/tmp/mysql.sock\n'
    else
      printf 'protocol=TCP\nhost=%s\nssl-mode=VERIFY_IDENTITY\nssl-ca=/tls/ca.crt\n' "$host"
    fi
  } > "$path"
  chmod 600 "$path"
}
query() { mysql --defaults-extra-file="$1" --batch --skip-column-names --silent; }
# Do not print MySQL errors for SQL containing credentials.
private_sql() {
  if ! query "$1" > /dev/null 2>"/tmp/sql-error-$$"; then
    rm -f "/tmp/sql-error-$$"
    fail 'private setup SQL rejected; inspect server state before retry'
  fi
  rm -f "/tmp/sql-error-$$"
}
member_clients() {
  [[ ${PRIMARY_MEMBER:-} == mysql-0 || ${PRIMARY_MEMBER:-} == mysql-1 ]] || fail 'invalid primary member'
  REPLICA_MEMBER=mysql-1
  [[ $PRIMARY_MEMBER == mysql-1 ]] && REPLICA_MEMBER=mysql-0
  PRIMARY_HOST="$PRIMARY_MEMBER.mysql-members.mysql.svc.cluster.local"
  REPLICA_HOST="$REPLICA_MEMBER.mysql-members.mysql.svc.cluster.local"
  P=/tmp/primary-$$.cnf
  R=/tmp/replica-$$.cnf
  client_file cloud_admin "$PRIMARY_HOST" "$P" admin
  client_file cloud_admin "$REPLICA_HOST" "$R" admin
  trap 'rm -f "$P" "$R" /tmp/app-$$.cnf' EXIT
}
wait_query() {
  local config=$1 sql=$2 expected=$3 result
  for ((i=0; i<90; i++)); do
    result=$(printf '%s\n' "$sql" | query "$config" 2>/dev/null) || result=''
    [[ $result == "$expected" ]] && return 0
    sleep 2
  done
  fail 'database did not reach expected state'
}
