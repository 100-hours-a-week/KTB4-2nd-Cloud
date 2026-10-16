#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]]
install -d -m 0755 /opt/yeodam/plg/targets
printf '%s' "$DISCOVERY_B64" | base64 -d > /usr/local/sbin/yeodam-discover-gpu-state
chmod 0755 /usr/local/sbin/yeodam-discover-gpu-state
cat > /etc/systemd/system/yeodam-gpu-discovery.service <<UNIT
[Unit]
Description=Read staging GPU EC2 lifecycle for Prometheus discovery
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/yeodam-discover-gpu-state --instance-id $GPU_INSTANCE_ID
TimeoutStartSec=30
NoNewPrivileges=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/opt/yeodam/plg/targets
UNIT
cat > /etc/systemd/system/yeodam-gpu-discovery.timer <<'UNIT'
[Unit]
Description=Refresh GPU lifecycle every 30 seconds
[Timer]
OnBootSec=15
OnUnitActiveSec=30
AccuracySec=2
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl start yeodam-gpu-discovery.service
systemctl enable --now yeodam-gpu-discovery.timer
