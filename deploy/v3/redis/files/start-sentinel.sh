#!/bin/bash
source /runtime/common.sh
[[ ${POD_NAME:-} =~ ^sentinel-[012]$ ]] || fail 'unexpected Sentinel member'
{
 printf 'user default off\n'
 printf 'user admin on #%s ~* &* +@all\n' "$(hash_password admin)"
 printf 'user sentinel-peer on #%s +ping +sentinel +client|setname +client|setinfo\n' "$(hash_password sentinel-peer)"
 printf 'user sentinel-client on #%s +ping +hello +role +client|setname +client|setinfo +client|id +sentinel|get-master-addr-by-name +sentinel|master +sentinel|masters +sentinel|replicas +sentinel|sentinels +sentinel|ckquorum\n' "$(hash_password sentinel-client)"
 printf 'user health on #%s +ping +sentinel|master +sentinel|ckquorum\n' "$(hash_password health)"
} > /tmp/sentinel-users.acl
if [[ ! -f /data/sentinel.conf ]]; then
 if [[ ${INITIAL_BOOTSTRAP:-false} == true ]]; then selected=redis-0.redis-members.redis.svc.cluster.local
 else selected=$(wait_primary); fi
 {
  printf 'bind 0.0.0.0\nprotected-mode yes\nport 0\ntls-port 26379\n'
  printf 'tls-cert-file /credentials/tls.crt\ntls-key-file /credentials/tls.key\ntls-ca-cert-file /credentials/ca.crt\n'
  printf 'tls-auth-clients no\ntls-replication yes\ntls-protocols "TLSv1.2 TLSv1.3"\n'
  printf 'daemonize no\nlogfile ""\nloglevel notice\ndir /data\naclfile /tmp/sentinel-users.acl\n'
  printf 'sentinel resolve-hostnames yes\nsentinel announce-hostnames yes\n'
  printf 'sentinel announce-ip %s.redis-sentinel.redis.svc.cluster.local\nsentinel announce-port 26379\n' "$POD_NAME"
  printf 'sentinel monitor %s %s 6379 2\n' "$MASTER_NAME" "$selected"
  printf 'sentinel auth-user %s monitor\nsentinel auth-pass %s %s\n' "$MASTER_NAME" "$MASTER_NAME" "$(password monitor)"
  printf 'sentinel sentinel-user sentinel-peer\nsentinel sentinel-pass %s\n' "$(password sentinel-peer)"
  printf 'sentinel down-after-milliseconds %s 10000\n' "$MASTER_NAME"
  printf 'sentinel failover-timeout %s 60000\n' "$MASTER_NAME"
  printf 'sentinel parallel-syncs %s 1\n' "$MASTER_NAME"
 } > /data/sentinel.conf.new
 mv /data/sentinel.conf.new /data/sentinel.conf
fi
exec redis-server /data/sentinel.conf --sentinel
