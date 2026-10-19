#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'Run as root on the reviewed shared GPU' >&2; exit 1; }
[[ "${EXPECTED_INSTANCE_ID:-}" =~ ^i-[0-9a-f]+$ ]] || { echo 'Missing expected GPU instance ID' >&2; exit 1; }
[[ "${EXPECTED_DATA_VOLUME:-}" =~ ^vol-[0-9a-f]+$ ]] || { echo 'Missing expected Qdrant EBS ID' >&2; exit 1; }
token=$(curl -fsS --noproxy '*' --connect-timeout 2 --max-time 3 -X PUT \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token)
actual=$(curl -fsS --noproxy '*' --connect-timeout 2 --max-time 3 \
  -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/instance-id)
[[ "$actual" == "$EXPECTED_INSTANCE_ID" ]] || { echo 'Wrong shared GPU host' >&2; exit 1; }
mountpoint -q /data/qdrant || { echo 'Qdrant data EBS is not mounted' >&2; exit 1; }
device=$(findmnt -n -o SOURCE --target /data/qdrant)
serial=$(lsblk -dn -o SERIAL "$device" | tr -d '[:space:]-' | tr '[:upper:]' '[:lower:]')
expected_serial=$(printf '%s' "$EXPECTED_DATA_VOLUME" | tr -d '-' | tr '[:upper:]' '[:lower:]')
[[ "$serial" == "$expected_serial" ]] || { echo 'Wrong Qdrant data EBS' >&2; exit 1; }
[[ "${EXPECTED_BIND_IP:-}" == 10.30.10.162 && -n "${PRODUCTION_QDRANT_COMPOSE_B64:-}" ]] || { echo 'Missing reviewed production Qdrant settings' >&2; exit 1; }

project=yeodam-v2-production-qdrant
container=yeodam-ai-qdrant-production
runtime=/opt/yeodam-production-qdrant
storage=/data/qdrant/production/storage
marker="$runtime/.initialized-instance"
if docker inspect "$container" >/dev/null 2>&1; then
  [[ "$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$container")" == "$project" ]] || { echo 'Unexpected container with production Qdrant name' >&2; exit 1; }
elif [[ -n "$(ss -H -lnt 'sport = :6335')" ]]; then
  echo 'Production Qdrant port is already occupied' >&2; exit 1
fi
if [[ -f "$marker" ]]; then
  [[ "$(cat "$marker")" == "$EXPECTED_INSTANCE_ID $EXPECTED_DATA_VOLUME" ]] || { echo 'Production storage marker mismatch' >&2; exit 1; }
elif [[ -d "$storage" && -n "$(find "$storage" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo 'Existing production storage requires explicit recovery' >&2; exit 1
fi
install -d -m 0750 "$runtime"
printf '%s' "$PRODUCTION_QDRANT_COMPOSE_B64" | base64 --decode > "$runtime/compose.yml"
printf 'AI_GPU_BIND_IP=%s\n' "$EXPECTED_BIND_IP" > "$runtime/.env"
cd "$runtime"
docker compose config --quiet
docker compose pull
image='qdrant/qdrant:v1.19.1@sha256:12364fe851b9f17356fc88189fc06d1b521262e04659ec7345975b00c9246a10'
uid=$(docker run --rm --network none --entrypoint id "$image" -u)
gid=$(docker run --rm --network none --entrypoint id "$image" -g)
# Operate on the new directory only; never chown/delete the staging storage tree.
install -d -m 0755 /data/qdrant/production
install -d -m 0750 -o "$uid" -g "$gid" "$storage"
printf '%s %s\n' "$EXPECTED_INSTANCE_ID" "$EXPECTED_DATA_VOLUME" > "$marker"
chmod 0600 "$marker"
cat > /etc/systemd/system/yeodam-production-qdrant.service <<'UNIT'
[Unit]
Description=Production Qdrant on the shared GPU host
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target
RequiresMountsFor=/data/qdrant
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/yeodam-production-qdrant
ExecStartPre=/usr/bin/mountpoint -q /data/qdrant
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose stop
TimeoutStartSec=120
TimeoutStopSec=90
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now yeodam-production-qdrant
for attempt in $(seq 1 30); do
  if curl -fsS --max-time 3 "http://$EXPECTED_BIND_IP:6335/readyz" >/dev/null; then
    echo 'Production Qdrant ready; staging containers and storage preserved'
    exit 0
  fi
  sleep 2
done
docker compose logs --tail 30 qdrant
exit 1
