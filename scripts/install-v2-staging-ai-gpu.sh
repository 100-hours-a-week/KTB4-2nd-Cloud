#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'Run as root on the staging GPU EC2' >&2; exit 1; }
mountpoint -q /data/qdrant || { echo 'Qdrant EBS is not mounted' >&2; exit 1; }
[[ -f /var/lib/yeodam-ai-gpu-host-ready ]] || { echo 'GPU host initialization is incomplete' >&2; exit 1; }
command -v aws >/dev/null
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
install -d -m 0750 /opt/yeodam-ai
install -d -m 0700 /etc/yeodam/ai
install -d -m 0755 /data/qdrant/storage /var/cache/yeodam/huggingface
printf '%s' "$AI_GPU_COMPOSE_B64" | base64 --decode > /opt/yeodam-ai/compose.yml
printf '%s' "$AI_GPU_SETTINGS_B64" | base64 --decode > /opt/yeodam-ai/configure-runtime.py
private_ip=$(ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
[[ -n "$private_ip" ]]
printf 'AI_GPU_BIND_IP=%s\n' "$private_ip" > /opt/yeodam-ai/.env
# Use an ephemeral registry login; runtime env files remain root-only on host.
export DOCKER_CONFIG
DOCKER_CONFIG=$(mktemp -d /run/yeodam-ai-docker.XXXXXX)
trap 'rm -rf "$DOCKER_CONFIG"' EXIT
python3 /opt/yeodam-ai/configure-runtime.py
cd /opt/yeodam-ai
docker compose config --quiet
docker compose pull
docker compose up -d embedding qdrant
# First start downloads model weights; do not mark it ready from a running PID.
for attempt in $(seq 1 240); do
  if curl --fail --silent --max-time 5 "http://$private_ip:8000/health" >/dev/null && \
     curl --fail --silent --max-time 5 "http://$private_ip:6333/readyz" >/dev/null; then
    break
  fi
  if [[ "$attempt" == 240 ]]; then
    echo 'Embedding or Qdrant readiness timed out; inspect container logs' >&2
    exit 1
  fi
  sleep 5
done
docker compose up -d process-runner
cat > /etc/systemd/system/yeodam-ai-gpu.service <<'UNIT'
[Unit]
Description=Yeodam staging embedding, Qdrant and process runner
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target
RequiresMountsFor=/data/qdrant
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/yeodam-ai
ExecStartPre=/usr/bin/mountpoint -q /data/qdrant
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose stop
TimeoutStartSec=900
TimeoutStopSec=90
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now yeodam-ai-gpu
# No real jobs are submitted by this installer.
docker compose ps --format json | python3 -c 'import json,sys; [print(r["Service"],r["State"],r.get("Health","")) for r in map(json.loads,sys.stdin) ]'
echo 'GPU runtime deployed; application job validation is separate'
