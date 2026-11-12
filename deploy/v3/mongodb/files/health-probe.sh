#!/bin/bash
set -Eeuo pipefail
# No driver, password, network connection or database query in a kubelet exec.
directory=/tmp/mongodb-health
read -r server < /proc/1/comm
[[ $server == mongod ]]
read -r monitor < "$directory/monitor.pid"
read -r observed pid healthy extra < "$directory/status"
[[ $monitor =~ ^[1-9][0-9]*$ && $pid == "$monitor" && $healthy == 1 && -z ${extra:-} ]]
[[ $observed =~ ^[0-9]{10}$ ]]
kill -0 "$monitor" 2>/dev/null
state=
while read -r field value rest; do
  if [[ $field == State: ]]; then state=$value; break; fi
done < "/proc/$monitor/status"
[[ -n $state && $state != Z && $state != X ]]
now=$(date +%s)
(( now >= observed && now - observed <= 30 ))
