#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || exit 1
install -d -m 0755 /opt/yeodam/plg/targets
printf '%s' "$DISCOVERY_B64" | base64 -d > /usr/local/sbin/yeodam-discover-backend-tasks
chmod 0755 /usr/local/sbin/yeodam-discover-backend-tasks
cat > /etc/systemd/system/yeodam-backend-discovery.service <<'UNIT'
[Unit]
Description=Refresh staging backend Prometheus targets from ECS
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/yeodam-discover-backend-tasks
TimeoutStartSec=90
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/yeodam/plg/targets
UNIT
cat > /etc/systemd/system/yeodam-backend-discovery.timer <<'UNIT'
[Unit]
Description=Refresh staging backend targets every 30 seconds
[Timer]
OnBootSec=15
OnUnitActiveSec=30
AccuracySec=2
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl start yeodam-backend-discovery.service
systemctl enable --now yeodam-backend-discovery.timer
