#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
source /etc/yeodam/mysql-backup.env

exec 9>/run/lock/yeodam-mysql-binlog-shipping.lock
flock -n 9 || { echo 'Binary log shipping is already running' >&2; exit 1; }

server_uuid=$(docker exec "$MYSQL_CONTAINER" sh -ec \
  'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot -NBe "SELECT @@server_uuid"')
[[ "$server_uuid" =~ ^[0-9a-fA-F-]{36}$ ]] || { echo 'Invalid MySQL server UUID' >&2; exit 1; }

docker exec "$MYSQL_CONTAINER" sh -ec \
  'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot -e "FLUSH BINARY LOGS"'
log_list=$(docker exec "$MYSQL_CONTAINER" sh -ec \
  'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot -NBe "SHOW BINARY LOGS"')
mapfile -t log_files < <(printf '%s\n' "$log_list" | awk '{ print $1 }')
(( ${#log_files[@]} >= 2 )) || { echo 'No closed binary log found' >&2; exit 1; }

install -d -m 0700 /var/lib/yeodam
state_file=/var/lib/yeodam/mysql-binlog-last-uploaded
last_number=-1
if [[ -s "$state_file" ]]; then
  read -r last_uuid last_file < "$state_file"
  [[ "$last_uuid" == "$server_uuid" && "$last_file" =~ ^mysql-bin\.([0-9]+)$ ]] || {
    echo 'Binary log shipping state belongs to a different server or is invalid' >&2
    exit 1
  }
  last_number=$((10#${BASH_REMATCH[1]}))
fi

for (( i=0; i<${#log_files[@]}-1; i++ )); do
  filename=${log_files[$i]}
  [[ "$filename" =~ ^mysql-bin\.[0-9]+$ ]] || { echo "Invalid binary log name: $filename" >&2; exit 1; }
  number=$((10#${filename##*.}))
  (( number > last_number )) || continue
  if (( last_number >= 0 && number != last_number + 1 )); then
    echo "Binary log gap after ${last_number}: $filename" >&2
    exit 1
  fi
  path="${MYSQL_DATA_DIR}/${filename}"
  key="binlog/${server_uuid}/${filename}"
  test -s "$path"
  local_sha=$(sha256sum "$path" | awk '{print $1}')
  local_size=$(stat -c %s "$path")

  if aws s3api head-object --bucket "$BACKUP_BUCKET" --key "$key" --region "$AWS_REGION" >/dev/null 2>&1; then
    remote_sha=$(aws s3api head-object --bucket "$BACKUP_BUCKET" --key "$key" --region "$AWS_REGION" --query 'Metadata.sha256' --output text)
    remote_size=$(aws s3api head-object --bucket "$BACKUP_BUCKET" --key "$key" --region "$AWS_REGION" --query ContentLength --output text)
    [[ "$remote_sha" == "$local_sha" && "$remote_size" == "$local_size" ]] || {
      echo "S3 binary log differs from local file: $key" >&2
      exit 1
    }
  else
    aws s3 cp "$path" "s3://${BACKUP_BUCKET}/${key}" --region "$AWS_REGION" --sse AES256 --metadata "sha256=${local_sha}" --only-show-errors
    remote_sha=$(aws s3api head-object --bucket "$BACKUP_BUCKET" --key "$key" --region "$AWS_REGION" --query 'Metadata.sha256' --output text)
    remote_size=$(aws s3api head-object --bucket "$BACKUP_BUCKET" --key "$key" --region "$AWS_REGION" --query ContentLength --output text)
    [[ "$remote_sha" == "$local_sha" && "$remote_size" == "$local_size" ]] || {
      echo "S3 binary log verification failed: $key" >&2
      exit 1
    }
  fi
  printf '%s %s\n' "$server_uuid" "$filename" > "${state_file}.next"
  mv "${state_file}.next" "$state_file"
  last_number=$number
done

date +%s > /var/lib/yeodam/mysql-binlog-last-success
echo "All closed binary logs are in s3://${BACKUP_BUCKET}/binlog/${server_uuid}/"
