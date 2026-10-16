#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]]
command -v nvidia-smi >/dev/null
command -v docker >/dev/null
install -d -m 0755 /opt/yeodam-ai-observability /var/lib/yeodam/gpu-metrics
install -d -m 0755 -o 65534 -g 65534 /var/lib/yeodam/gpu-alloy
printf '%s' "$SAMPLER_B64" | base64 -d > /usr/local/sbin/yeodam-sample-ai-gpu
chmod 0755 /usr/local/sbin/yeodam-sample-ai-gpu
printf '%s' "$ALLOY_B64" | base64 -d > /opt/yeodam-ai-observability/ai-gpu.alloy
printf '%s' "$COMPOSE_B64" | base64 -d > /opt/yeodam-ai-observability/compose.yml
printf 'GPU_PRIVATE_IP=%s\nGPU_INSTANCE_ID=%s\n' "$GPU_PRIVATE_IP" "$GPU_INSTANCE_ID" > /opt/yeodam-ai-observability/.env
chmod 0644 /opt/yeodam-ai-observability/{ai-gpu.alloy,compose.yml,.env}
cat > /etc/systemd/system/yeodam-gpu-sample.service <<UNIT
[Unit]
Description=Read NVIDIA and fixed AI container state into Prometheus textfile
After=docker.service network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/yeodam-sample-ai-gpu --address $GPU_PRIVATE_IP
TimeoutStartSec=65
NoNewPrivileges=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/yeodam/gpu-metrics
UNIT
cat > /etc/systemd/system/yeodam-gpu-sample.timer <<'UNIT'
[Unit]
Description=Sample AI GPU host every 15 seconds
[Timer]
OnBootSec=15
OnUnitActiveSec=15
AccuracySec=1
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl start yeodam-gpu-sample.service
systemctl enable --now yeodam-gpu-sample.timer
cd /opt/yeodam-ai-observability
docker compose pull --quiet
docker compose run -T --interactive=false --rm --no-deps alloy validate /etc/alloy/config.alloy
docker compose up -d --force-recreate alloy
ready=0
for attempt in {1..15}; do
  if curl --fail --silent --max-time 2 "http://$GPU_PRIVATE_IP:12345/-/ready" >/dev/null; then
    ready=1
    break
  fi
  sleep 2
done
[[ "$ready" == 1 ]] || { echo 'GPU metrics agent did not become ready' >&2; exit 1; }
printf 'AI GPU observability installed; application containers were not redeployed.\n'
