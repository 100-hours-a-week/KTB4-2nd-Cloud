#!/bin/bash
source /runtime/common.sh
if [[ ${PROCESS_KIND:-} == redis ]]; then
 [[ $(cli 127.0.0.1 6379 health health PING 2>/dev/null) == PONG ]] || exit 1
 if [[ ${1:-} == readiness ]]; then
  info=$(cli 127.0.0.1 6379 health health INFO persistence 2>/dev/null | tr -d '\r')
  [[ $info == *'loading:0'* && $info == *'aof_last_write_status:ok'* ]] || exit 1
  role=$(cli 127.0.0.1 6379 health health ROLE 2>/dev/null | head -n1)
  expected=$(awk '/^replicaof /{replica=1} END{print replica ? "slave" : "master"}' /data/redis.conf)
  [[ $role == "$expected" ]] || exit 1
  if [[ $role == slave ]]; then
   replication=$(cli 127.0.0.1 6379 health health INFO replication 2>/dev/null | tr -d '\r')
   [[ $replication == *'master_link_status:up'* ]]
  fi
 fi
else
 [[ $(cli 127.0.0.1 26379 health health PING 2>/dev/null) == PONG ]] || exit 1
 if [[ ${1:-} != liveness ]]; then
  info=$(cli 127.0.0.1 26379 health health SENTINEL master "$MASTER_NAME" 2>/dev/null)
  [[ $info == *"$MASTER_NAME"* ]]
 fi
fi
# Replication links and CKQUORUM failures never trigger liveness restarts.
