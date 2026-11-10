set -Eeuo pipefail
# The caller executes this only inside the selected Redis primary Pod.
export REDISCLI_AUTH="$(cat /credentials/admin-password)"
cli() { redis-cli --no-auth-warning --tls --cacert /credentials/ca.crt --sni 127.0.0.1 -h 127.0.0.1 --user admin --raw -e "$@"; }
[[ $(cli ROLE | head -1) == master ]] || exit 1
case "$1" in
 backup) redis-cli --no-auth-warning --tls --cacert /credentials/ca.crt --sni 127.0.0.1 -h 127.0.0.1 --user admin --rdb /tmp/cutover-602.rdb >/dev/null 2>&1; cat /tmp/cutover-602.rdb; rm -f /tmp/cutover-602.rdb;;
 clear) [[ $(cli INFO keyspace | tr -d '\r' | grep '^db' | cut -d: -f1 | grep -v '^db0$' | wc -l) == 0 ]] || exit 1; [[ $(cli FLUSHDB SYNC) == OK && $(cli DBSIZE) == 0 ]] || exit 1; printf 'CLEARED';;
 queue-count) keys=$(cli --scan --type zset); count=0; while IFS= read -r key; do [[ -z $key ]] || count=$((count+$(cli ZCARD "$key"))); done <<< "$keys"; keys=$(cli --scan --type stream); while IFS= read -r key; do [[ -z $key ]] || count=$((count+$(cli XLEN "$key"))); done <<< "$keys"; printf '%s' "$count";;
 *) exit 1;;
esac
