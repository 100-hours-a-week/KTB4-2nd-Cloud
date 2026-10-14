#!/usr/bin/env bash
set -Eeuo pipefail
set +x

parameter_name=/yeodam/v2/staging/v1-source/app/MYSQL_PASSWORD
mysql_container=yeodam-v2-mysql
app_user=yeodam_app
app_host=${V1_APP_PRIVATE_IP:-}

[[ "$app_host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo 'V1_APP_PRIVATE_IP must be the V1 staging App private IPv4 address' >&2
  exit 1
}

password=$(aws ssm get-parameter --region ap-northeast-2 \
  --name "$parameter_name" --with-decryption --query Parameter.Value --output text)
[[ "$password" =~ ^[0-9a-fA-F]{64}$ ]] || {
  echo 'V1 staging MYSQL_PASSWORD must be 64 hexadecimal characters' >&2
  exit 1
}

# MySQL prefers this exact host over the 10.30.10.% V2 Task account. The
# existing V1 App secret stays in Parameter Store, not Terraform state.
printf "CREATE USER IF NOT EXISTS '%s'@'%s' IDENTIFIED BY '%s';\nALTER USER '%s'@'%s' IDENTIFIED BY '%s';\nGRANT ALL PRIVILEGES ON yeodam_v1_source.* TO '%s'@'%s';\n" \
  "$app_user" "$app_host" "$password" \
  "$app_user" "$app_host" "$password" \
  "$app_user" "$app_host" | \
  sudo docker exec -i "$mysql_container" sh -ec \
    'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot'
unset password

count=$(sudo docker exec "$mysql_container" sh -ec \
  "export MYSQL_PWD=\$(cat /run/secrets/mysql-root-password); exec mysql -uroot -NBe \"SELECT COUNT(*) FROM mysql.user WHERE User='${app_user}' AND Host='${app_host}'\"")
[[ "$count" == 1 ]] || { echo 'V1 staging app account was not provisioned' >&2; exit 1; }
echo 'V1 staging App MySQL account is provisioned on the V2 staging host.'
