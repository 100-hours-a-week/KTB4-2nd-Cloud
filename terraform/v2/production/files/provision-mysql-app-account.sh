#!/usr/bin/env bash
set +x
set -Eeuo pipefail

# Check the host before reading any secret or issuing DDL.
[[ "${EXPECTED_INSTANCE_ID:-}" =~ ^i-[0-9a-f]+$ ]] || { echo 'Missing expected MySQL instance ID' >&2; exit 1; }
token=$(curl --fail --silent --show-error --noproxy '*' --connect-timeout 2 --max-time 3 \
  -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token)
actual_id=$(curl --fail --silent --show-error --noproxy '*' --connect-timeout 2 --max-time 3 \
  -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/instance-id)
[ "$actual_id" = "$EXPECTED_INSTANCE_ID" ] || { echo 'Wrong MySQL host' >&2; exit 1; }

password=$(aws ssm get-parameter --region ap-northeast-2 \
  --name /yeodam/v2/production/app/MYSQL_PASSWORD --with-decryption --query Parameter.Value --output text)
[[ "$password" =~ ^[0-9a-f]{64}$ ]] || { echo 'Invalid application credential format' >&2; exit 1; }

# Refuse to alter accounts if this is already a replica or a nonempty production DB.
docker exec yeodam-v2-mysql sh -ec \
  'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot -NBe "SELECT COUNT(*) FROM information_schema.tables WHERE TABLE_SCHEMA=\"yeodam\"; SELECT COUNT(*) FROM performance_schema.replication_connection_configuration;"' \
  | awk 'NR==1 && $1!=0 {exit 1} NR==2 && $1!=0 {exit 1} END {if (NR!=2) exit 1}'

printf "CREATE USER IF NOT EXISTS 'yeodam_app'@'10.20.10.%%' IDENTIFIED BY '%s';\nALTER USER 'yeodam_app'@'10.20.10.%%' IDENTIFIED BY '%s';\nGRANT ALL PRIVILEGES ON yeodam.* TO 'yeodam_app'@'10.20.10.%%';\n" \
  "$password" "$password" | docker exec -i yeodam-v2-mysql sh -ec \
    'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot'
unset password
echo 'New production MySQL application account provisioned; no V1 data imported.'
