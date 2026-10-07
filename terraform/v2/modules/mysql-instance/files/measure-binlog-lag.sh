#!/usr/bin/env bash
set -Eeuo pipefail
source /etc/yeodam/mysql-backup.env

now=$(date +%s)
last_success=0
if [[ -s /var/lib/yeodam/mysql-binlog-last-success ]]; then
  read -r last_success < /var/lib/yeodam/mysql-binlog-last-success
fi
[[ "$last_success" =~ ^[0-9]+$ ]] || last_success=0
age=$((now - last_success))
(( age >= 0 )) || age=0

aws cloudwatch put-metric-data \
  --region "$AWS_REGION" \
  --namespace Yeodam/V2/Staging/MySQL \
  --metric-name ExternalBinlogAgeSeconds \
  --unit Seconds \
  --value "$age"
