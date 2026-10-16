#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]]
printf '%s' "$PUBLISHER_B64" | base64 -d > /usr/local/sbin/yeodam-publish-plg-health
chmod 0755 /usr/local/sbin/yeodam-publish-plg-health
cat > /etc/systemd/system/yeodam-plg-health.service <<UNIT
[Unit]
Description=Publish local PLG readiness and scrape health to independent CloudWatch alarm
After=network-online.target docker.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/yeodam-publish-plg-health --instance-id $PLG_INSTANCE_ID
TimeoutStartSec=45
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
UNIT
cat > /etc/systemd/system/yeodam-plg-health.timer <<'UNIT'
[Unit]
Description=Publish PLG external heartbeat every minute
[Timer]
OnBootSec=15
OnUnitActiveSec=60
AccuracySec=2
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl start yeodam-plg-health.service
systemctl enable --now yeodam-plg-health.timer
printf 'PLG external heartbeat installed; no service containers restarted.\n'
