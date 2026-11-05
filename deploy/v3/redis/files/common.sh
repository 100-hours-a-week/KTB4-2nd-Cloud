#!/bin/bash
set -Eeuo pipefail
umask 077
MASTER_NAME=yeodam-v3-app-redis
fail() { echo "Redis check failed: $*" >&2; exit 1; }
password() {
  local value
  value=$(cat "/credentials/$1-password")
  [[ $value =~ ^[a-f0-9]{48}$ ]] || fail 'invalid credential format'
  printf '%s' "$value"
}
hash_password() { password "$1" | sha256sum | cut -d' ' -f1; }
cli() {
  local host=$1 port=$2 user=$3 credential=$4
  shift 4
  REDISCLI_AUTH="$(password "$credential")" timeout 4 redis-cli --no-auth-warning \
    --tls --cacert /credentials/ca.crt --sni "$host" -h "$host" -p "$port" \
    --user "$user" --raw -e "$@"
}
# Require agreement from at least two healthy monitors, never accept arbitrary hosts.
discover_primary() {
  local zero=0 one=0 host reply flags name
  for index in 0 1 2; do
    host="sentinel-$index.redis-sentinel.redis.svc.cluster.local"
    reply=$(cli "$host" 26379 sentinel-client sentinel-client SENTINEL get-master-addr-by-name "$MASTER_NAME" 2>/dev/null) || continue
    name=${reply%%$'\n'*}
    [[ $name == redis-0.redis-members.redis.svc.cluster.local || $name == redis-1.redis-members.redis.svc.cluster.local ]] || continue
    [[ ${reply#*$'\n'} == 6379 ]] || continue
    flags=$(cli "$host" 26379 sentinel-client sentinel-client SENTINEL master "$MASTER_NAME" 2>/dev/null | awk '/^flags$/{getline; print; exit}') || continue
    [[ $flags == master ]] || continue
    if [[ $name == redis-0.redis-members.redis.svc.cluster.local ]]; then zero=$((zero+1))
    else one=$((one+1)); fi
  done
  if [[ $zero -ge 2 ]]; then printf '%s' redis-0.redis-members.redis.svc.cluster.local
  elif [[ $one -ge 2 ]]; then printf '%s' redis-1.redis-members.redis.svc.cluster.local
  else return 1; fi
}
wait_primary() {
  local selected
  for ((attempt=0; attempt<60; attempt++)); do
    selected=$(discover_primary) && { printf '%s' "$selected"; return 0; }
    sleep 2
  done
  fail 'no healthy Sentinel majority; manual recovery may be required'
}
