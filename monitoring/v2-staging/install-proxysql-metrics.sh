#!/usr/bin/env bash
set -Eeuo pipefail
set +x
[[ $EUID -eq 0 ]] || { echo 'Run on the staging App as root' >&2; exit 1; }
: "${METRICS_SCRIPT_B64:?}"
[[ -f /etc/yeodam/v2-staging-proxysql/admin-password ]] || { echo 'ProxySQL is not installed' >&2; exit 1; }
install -d -m 0755 /usr/local/sbin /var/lib/yeodam/mysql-metrics
printf '%s' "$METRICS_SCRIPT_B64" | base64 -d > /usr/local/sbin/yeodam-proxysql-metrics.py
chmod 0750 /usr/local/sbin/yeodam-proxysql-metrics.py
unset METRICS_SCRIPT_B64
cat > /etc/systemd/system/yeodam-proxysql-metrics.service <<'UNIT'
[Unit]
Description=Read staging ProxySQL state for Alloy
After=docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/sbin/yeodam-proxysql-metrics.py
TimeoutStartSec=25
UNIT
cat > /etc/systemd/system/yeodam-proxysql-metrics.timer <<'UNIT'
[Unit]
Description=Sample staging ProxySQL every 5 seconds

[Timer]
OnBootSec=15s
OnUnitActiveSec=5s
AccuracySec=1s
Unit=yeodam-proxysql-metrics.service

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl start yeodam-proxysql-metrics.service
systemctl enable --now yeodam-proxysql-metrics.timer
echo 'ProxySQL read-only sampler enabled; existing Alloy forwards the textfile metrics.'
