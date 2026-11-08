#!/bin/sh
set -eu
export REDISCLI_AUTH="$(cat /credentials/health-password)"
reply=$(redis-cli --tls --cacert /credentials/ca.crt --sni ai-redis.ai-redis.svc.cluster.local -h 127.0.0.1 --user health --no-auth-warning PING)
[ "$reply" = PONG ]
