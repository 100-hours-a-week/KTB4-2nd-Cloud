#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  echo 'Usage: deploy-v2-staging-plg.sh --scope server|v1-source --release-dir DIR [--admin-secret-arn ARN]' >&2
  exit 2
}

scope=''
release_dir=''
admin_secret_arn=''
while (($#)); do
  case "$1" in
    --scope) scope="${2:-}"; shift 2 ;;
    --release-dir) release_dir="${2:-}"; shift 2 ;;
    --admin-secret-arn) admin_secret_arn="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done
[[ "$scope" == server || "$scope" == v1-source ]] || usage
[[ -d "$release_dir/monitoring/v2-staging" ]] || usage
[[ "$(id -u)" == 0 ]] || { echo 'Run as root on the target EC2 host' >&2; exit 1; }

source_dir="$release_dir/monitoring/v2-staging"
if [[ "$scope" == server ]]; then
  [[ -n "$admin_secret_arn" ]] || usage
  mountpoint -q /srv/yeodam/plg || { echo 'PLG data EBS is not mounted' >&2; exit 1; }
  install -d -m 0700 /etc/yeodam/plg
  umask 077
  aws secretsmanager get-secret-value \
    --secret-id "$admin_secret_arn" \
    --region ap-northeast-2 \
    --query SecretString --output text > /etc/yeodam/plg/admin-password.next
  [[ -s /etc/yeodam/plg/admin-password.next ]] || { echo 'Grafana admin secret has no version' >&2; exit 1; }
  chown 472:472 /etc/yeodam/plg/admin-password.next
  chmod 0400 /etc/yeodam/plg/admin-password.next
  mv /etc/yeodam/plg/admin-password.next /etc/yeodam/plg/admin-password

  install -d -m 0755 /opt/yeodam/plg
  cp -a "$source_dir/plg-compose.yml" "$source_dir/prometheus.yml" "$source_dir/loki.yml" /opt/yeodam/plg/
  cp -a "$source_dir/grafana" /opt/yeodam/plg/
  cd /opt/yeodam/plg
  docker compose -f plg-compose.yml config --quiet
  docker compose -f plg-compose.yml up -d
  for attempt in $(seq 1 30); do
    if curl --fail --silent http://127.0.0.1:3100/ready >/dev/null &&
       curl --fail --silent http://127.0.0.1:9090/-/ready >/dev/null &&
       curl --fail --silent http://127.0.0.1:3000/api/health >/dev/null; then
      echo 'PLG server is ready on its private host'
      exit 0
    fi
    sleep 5
  done
  docker compose -f plg-compose.yml ps
  echo 'PLG server did not become ready' >&2
  exit 1
fi

install -d -m 0755 /opt/yeodam/v1-source-observability /var/lib/yeodam/alloy
cp -a "$source_dir/v1-source.alloy" "$source_dir/v1-source-alloy-compose.yml" /opt/yeodam/v1-source-observability/
cd /opt/yeodam/v1-source-observability
docker compose -f v1-source-alloy-compose.yml config --quiet
docker compose -f v1-source-alloy-compose.yml run --rm alloy validate /etc/alloy/config.alloy
docker compose -f v1-source-alloy-compose.yml up -d
docker compose -f v1-source-alloy-compose.yml ps
