#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID == 0 ]]
mountpoint -q /srv/yeodam/plg
install -d -m 0755 /opt/yeodam/plg/targets /var/lib/node_exporter/textfile
install -d -m 0700 /etc/yeodam/plg
python3 - <<'PY'
import json,subprocess,os,pathlib
r=subprocess.check_output(['aws','secretsmanager','get-secret-value','--region','ap-northeast-2','--secret-id','yeodam/v2/production/plg/grafana-admin-password'],text=True)
password=json.loads(r)['SecretString'];assert len(password)>=32
p=pathlib.Path('/etc/yeodam/plg/admin-password');p.write_text(password);p.chmod(0o400);os.chown(p,472,472)
PY
bash /usr/local/lib/yeodam-observability/prepare-ai-metrics-secret.sh
export DISCOVERY_B64="$(base64 -w0 /usr/local/lib/yeodam-observability/discover-backend-tasks.py)"
bash /usr/local/lib/yeodam-observability/install-backend-discovery.sh
export DISCOVERY_B64="$(base64 -w0 /usr/local/lib/yeodam-observability/discover-gpu-state.py)"
bash /usr/local/lib/yeodam-observability/install-gpu-discovery.sh
cd /opt/yeodam/plg
docker compose -f plg-compose.yml config --quiet
docker compose -f plg-compose.yml pull --quiet
docker run --rm --network none --entrypoint /bin/promtool -v /opt/yeodam/plg:/etc/prometheus:ro -v /etc/yeodam/plg/ai-metrics:/run/secrets/ai-metrics:ro prom/prometheus:v3.14.0@sha256:5ce7540c3c00ef4ab0c9d2c995c6a5b9c421f44b4a115d97a2c7af3b1c21cbb0 check config /etc/prometheus/prometheus.yml
docker compose -f plg-compose.yml up -d
for _ in $(seq 1 25); do
 if curl -fsS http://127.0.0.1:3000/api/health >/dev/null && curl -fsS http://127.0.0.1:9090/-/ready >/dev/null && curl -fsS http://127.0.0.1:3100/ready >/dev/null; then
  export PUBLISHER_B64="$(base64 -w0 /usr/local/lib/yeodam-observability/publish-plg-health.py)"
  export PLG_INSTANCE_ID="$EXPECTED_INSTANCE_ID"
  bash /usr/local/lib/yeodam-observability/install-plg-health.sh
  echo 'Production PLG ready; no application service changed.'
  exit 0
 fi
 sleep 3
done
exit 1
