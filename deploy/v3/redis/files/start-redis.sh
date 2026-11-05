#!/bin/bash
source /runtime/common.sh
[[ ${POD_NAME:-} == redis-0 || ${POD_NAME:-} == redis-1 ]] || fail 'unexpected Redis member'
mkdir -p /data/db
own="$POD_NAME.redis-members.redis.svc.cluster.local"
fresh=false
if [[ ! -f /data/redis.conf ]]; then
  [[ -z $(ls -A /data/db) ]] || fail 'existing data without role configuration requires recovery'
  fresh=true
fi
if [[ $fresh == true && ${INITIAL_BOOTSTRAP:-false} == true ]]; then
  selected=redis-0.redis-members.redis.svc.cluster.local
else
  selected=$(wait_primary)
  [[ $fresh != true || $selected != "$own" ]] || fail 'empty selected primary requires data recovery'
fi
# Hash app/admin ACL passwords; replication auth must be writable in the retained config.
{
 printf 'user default off\n'
 printf 'user admin on #%s ~* &* +@all\n' "$(hash_password admin)"
 printf 'user app on #%s ~* &* +@connection +@read +@write +@transaction +@scripting +@pubsub -@admin -@dangerous +wait\n' "$(hash_password app)"
 printf 'user replication on #%s +ping +psync +replconf\n' "$(hash_password replication)"
 printf 'user monitor on #%s ~* &__sentinel__:hello +multi +slaveof +replicaof +ping +exec +subscribe +publish +info +role +config|rewrite +client|setname +client|kill +script|kill\n' "$(hash_password monitor)"
 printf 'user health on #%s +ping +info +role\n' "$(hash_password health)"
} > /tmp/users.acl
# Sentinel CONFIG REWRITE persists replicaof changes; restart still rechecks majority.
{
 printf 'include /runtime/redis-base.conf\n'
 printf 'masteruser replication\nmasterauth %s\n' "$(password replication)"
 printf 'replica-announce-ip %s\nreplica-announce-port 6379\n' "$own"
 if [[ $selected != "$own" ]]; then printf 'replicaof %s 6379\n' "$selected"; fi
} > /data/redis.conf.new
mv /data/redis.conf.new /data/redis.conf
exec redis-server /data/redis.conf
