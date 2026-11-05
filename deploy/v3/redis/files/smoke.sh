#!/bin/bash
source /runtime/common.sh
primary=$(wait_primary)
replica=redis-1.redis-members.redis.svc.cluster.local
[[ $primary == "$replica" ]] && replica=redis-0.redis-members.redis.svc.cluster.local
for index in 0 1 2; do
 host="sentinel-$index.redis-sentinel.redis.svc.cluster.local"
 [[ $(cli "$host" 26379 sentinel-client sentinel-client SENTINEL ckquorum "$MASTER_NAME") == OK* ]] || fail 'Sentinel quorum not healthy'
done
key="yeodam:cloud-check:523:$(date +%s):$$"
cleanup() { printf 'DEL %s\nWAIT 1 1000\n' "$key" | cli "$primary" 6379 app app >/dev/null 2>&1 || true; }
trap cleanup EXIT
# WAIT runs on the SAME client connection as SET; otherwise it would prove nothing.
reply=$(printf 'SET %s v3-replication EX 120 NX\nWAIT 1 2000\n' "$key" | cli "$primary" 6379 app app)
[[ $reply == $'OK\n1' ]] || fail 'trial write/replication acknowledgment failed'
[[ $(cli "$replica" 6379 app app GET "$key") == v3-replication ]] || fail 'replica data mismatch'
if cli "$replica" 6379 app app SET "$key" must-not-write >/dev/null 2>&1; then fail 'replica accepted write'; fi
if cli "$primary" 6379 app app CONFIG GET dir >/dev/null 2>&1; then fail 'app can access admin config'; fi
if REDISCLI_AUTH=synthetic-invalid timeout 4 redis-cli --no-auth-warning --tls --cacert /credentials/ca.crt --sni "$primary" -h "$primary" --user app --raw -e PING >/dev/null 2>&1; then fail 'bad password accepted'; fi
cleanup
[[ $(cli "$primary" 6379 app app EXISTS "$key") == 0 && $(cli "$replica" 6379 app app EXISTS "$key") == 0 ]] || fail 'trial cleanup failed'
trap - EXIT
echo 'PASS: healthy Sentinel quorum, TLS authenticated write, same-connection replica ACK, row/read-only/ACL checks and cleanup'
