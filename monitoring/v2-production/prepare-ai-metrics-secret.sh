#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'Run on the PLG host as root' >&2; exit 1; }
# Directory bind mount permits atomic key refresh without exposing values in config/state.
install -d -m 0750 -o 65534 -g 65534 /etc/yeodam/plg/ai-metrics
umask 077
next=$(mktemp /etc/yeodam/plg/ai-metrics/.api-key.XXXXXX)
trap 'rm -f "$next"' EXIT
aws ssm get-parameter --region ap-northeast-2 \
  --name /yeodam/v2/production/ai/API_KEY --with-decryption \
  --query Parameter.Value --output text > "$next"
[[ -s "$next" ]] || { echo 'AI metrics key is empty' >&2; exit 1; }
chown 65534:65534 "$next"
chmod 0400 "$next"
mv "$next" /etc/yeodam/plg/ai-metrics/api-key
