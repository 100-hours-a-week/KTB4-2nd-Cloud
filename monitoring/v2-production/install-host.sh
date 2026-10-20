#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID == 0 && "$OBS_KIND" =~ ^(mysql|redis|kafka|gpu|plg)$ ]]
install -d -m 0755 /var/lib/node_exporter/textfile /usr/local/lib/yeodam-observability
image='quay.io/prometheus/node-exporter:v1.12.1@sha256:1b4e4438faca4dd7e001dd445d161a4a2091b0fededa84093b3a8dfeae1f1be0'
docker pull -q "$image" >/dev/null
# Host metrics only. No application volumes/credentials or Docker socket in exporter.
if docker inspect yeodam-production-node-exporter >/dev/null 2>&1; then
 docker inspect --format '{{.Config.Image}}' yeodam-production-node-exporter | grep -Fx "$image" >/dev/null
else
 docker run -d --name yeodam-production-node-exporter --restart unless-stopped --network host --pid host \
  --read-only --cap-drop ALL --security-opt no-new-privileges --memory 96m --cpus .20 \
  -v /:/host:ro,rslave -v /var/lib/node_exporter/textfile:/textfile:ro "$image" \
  --path.rootfs=/host --web.listen-address="$OBS_PRIVATE_IP:9100" --collector.textfile.directory=/textfile \
  --collector.filesystem.mount-points-exclude='^/(dev|proc|sys|run|var/lib/docker|var/lib/containerd)($|/)' >/dev/null
fi
if [[ "$OBS_KIND" != plg ]]; then
 cat > /etc/systemd/system/yeodam-production-host-logs.service <<UNIT
[Unit]
Description=Ship fixed production container logs to Loki
After=docker.service network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/yeodam-observability/ship-host-logs.py --kind $OBS_KIND --instance $EXPECTED_INSTANCE_ID
TimeoutStartSec=40
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/yeodam/production-monitoring /var/lib/node_exporter/textfile
UNIT
 install -d -m 0700 /var/lib/yeodam/production-monitoring
 cat > /etc/systemd/system/yeodam-production-host-logs.timer <<'UNIT'
[Timer]
OnBootSec=30
OnUnitActiveSec=15
AccuracySec=2
[Install]
WantedBy=timers.target
UNIT
 systemctl daemon-reload
 systemctl enable --now yeodam-production-host-logs.timer
fi
if [[ "$OBS_KIND" == mysql ]]; then
 bash /usr/local/lib/yeodam-observability/install-mysql-validation-metrics.sh
 python3 - <<'PY'
from pathlib import Path
import subprocess,secrets,re
base=Path('/etc/yeodam/production-monitoring');base.mkdir(mode=0o700,parents=True,exist_ok=True)
p=base/'pipeline-password';password=p.read_text().strip() if p.exists() else secrets.token_hex(32);assert re.fullmatch('[0-9a-f]{64}',password)
query=f"SET SESSION sql_log_bin=0; CREATE USER IF NOT EXISTS 'yeodam_pipeline_metrics'@'localhost' IDENTIFIED BY '{password}' WITH MAX_USER_CONNECTIONS 2; ALTER USER 'yeodam_pipeline_metrics'@'localhost' IDENTIFIED BY '{password}' WITH MAX_USER_CONNECTIONS 2;"
for table in ['trip_creation_outbox','additional_attachment_upload_outbox','story_generation_outbox']:
 query+=f"GRANT SELECT (status,cdc_managed,created_at) ON yeodam.{table} TO 'yeodam_pipeline_metrics'@'localhost';"
query+="GRANT SELECT (status,result_received_at) ON yeodam.trip_creation_executions TO 'yeodam_pipeline_metrics'@'localhost';"
r=subprocess.run(['docker','exec','-i','yeodam-v2-mysql','sh','-ec','export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot'],input=query,text=True,capture_output=True)
assert r.returncode==0,'Read-only pipeline account failed (SQL withheld)'
p.write_text(password+'\n');p.chmod(0o600)
PY
elif [[ "$OBS_KIND" == redis ]]; then
 bash /usr/local/lib/yeodam-observability/install-ai-redis-metrics.sh
elif [[ "$OBS_KIND" == kafka ]]; then
 bash /usr/local/lib/yeodam-observability/install-kafka-observability.sh
elif [[ "$OBS_KIND" == gpu ]]; then
 bash /usr/local/lib/yeodam-observability/install-ai-gpu-metrics.sh
fi
if [[ "$OBS_KIND" == mysql || "$OBS_KIND" == kafka ]]; then
 kind=mysql; [[ "$OBS_KIND" != kafka ]] || kind=connect
 cat > /etc/systemd/system/yeodam-production-probe.service <<UNIT
[Unit]
Description=Fixed read-only production pipeline health metrics
After=docker.service network-online.target
[Service]
ExecStart=/usr/bin/python3 /usr/local/lib/yeodam-observability/probe-exporter.py --kind $kind --address $OBS_PRIVATE_IP
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
MemoryMax=64M
CPUQuota=20%
[Install]
WantedBy=multi-user.target
UNIT
 systemctl daemon-reload
 systemctl enable --now yeodam-production-probe.service
fi
printf 'Production %s observers installed; application services preserved.\n' "$OBS_KIND"
