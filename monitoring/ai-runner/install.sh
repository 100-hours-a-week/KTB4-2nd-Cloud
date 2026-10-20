#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID == 0 ]]
# Host identity comes from IMDSv2. No application containers are recreated here.
token=$(curl -fsS -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token)
actual=$(curl -fsS -H "X-aws-ec2-metadata-token: $token" http://169.254.169.254/latest/meta-data/instance-id)
[[ "$actual" == i-05e1ad9dd96dba4ae ]]
agent=/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl
[[ -x "$agent" ]]
install -d -m 0700 /var/lib/yeodam/ai-runner-logs /var/lib/yeodam/ai-runner-level
install -d -m 0750 /var/log/yeodam/ai-runner
chmod 0755 /usr/local/lib/yeodam-ai-runner-ops/level.py
ln -sfn /usr/local/lib/yeodam-ai-runner-ops/level.py /usr/local/sbin/yeodam-ai-runner-log-level
cat > /etc/systemd/system/yeodam-ai-runner-logs.service <<'UNIT'
[Unit]
Description=Normalize fixed staging and production process runner logs
After=docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/yeodam-ai-runner-ops/logs.py
TimeoutStartSec=30
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/yeodam/ai-runner-logs /var/log/yeodam/ai-runner
MemoryMax=128M
CPUQuota=25%
UNIT
cat > /etc/systemd/system/yeodam-ai-runner-logs.timer <<'UNIT'
[Timer]
OnBootSec=20
OnUnitActiveSec=5
AccuracySec=1
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl start yeodam-ai-runner-logs.service
systemctl enable --now yeodam-ai-runner-logs.timer
# Append a dedicated fragment; preserve other CloudWatch Agent inputs.
"$agent" -a append-config -m ec2 -s -c file:/usr/local/lib/yeodam-ai-runner-ops/cloudwatch.json >/var/log/yeodam/ai-runner-agent-apply.log 2>&1
systemctl is-active --quiet amazon-cloudwatch-agent
printf 'Runner log collection configured. Application containers preserved.\n'
