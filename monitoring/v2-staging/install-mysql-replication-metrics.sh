#!/usr/bin/env bash
set -Eeuo pipefail
set +x

role=${1:-}
[[ "$role" == source || "$role" == replica ]] || {
  echo 'Usage: install-mysql-replication-metrics.sh source|replica' >&2
  exit 2
}
[[ $(id -u) == 0 ]] || { echo 'Run as root' >&2; exit 1; }
: "${METRICS_SCRIPT_B64:?}"
: "${ALLOY_CONFIG_B64:?}"

if [[ "$role" == source ]]; then
  : "${ALLOY_COMPOSE_B64:?}"
  directory=/opt/yeodam/v1-source-observability
  config=v1-source.alloy
  compose=v1-source-alloy-compose.yml
else
  directory=/opt/yeodam/mysql-replication-observability
  config=mysql-replica.alloy
  compose=mysql-replica-alloy-compose.yml
fi

install -d -m 0755 "$directory" /var/lib/yeodam/mysql-metrics
install -d -m 0755 /usr/local/sbin
printf '%s' "$METRICS_SCRIPT_B64" | base64 -d > /usr/local/sbin/yeodam-mysql-replication-metrics
printf '%s' "$ALLOY_CONFIG_B64" | base64 -d > "$directory/$config"
chmod 0750 /usr/local/sbin/yeodam-mysql-replication-metrics
chmod 0644 "$directory/$config"
if [[ "$role" == source ]]; then
  printf '%s' "$ALLOY_COMPOSE_B64" | base64 -d > "$directory/$compose"
  chmod 0644 "$directory/$compose"
fi
unset METRICS_SCRIPT_B64 ALLOY_CONFIG_B64 ALLOY_COMPOSE_B64

cat > /etc/systemd/system/yeodam-mysql-replication-metrics.service <<EOF
[Unit]
Description=Sample Yeodam staging MySQL $role replication metrics
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/yeodam-mysql-replication-metrics $role
TimeoutStartSec=20
EOF

cat > /etc/systemd/system/yeodam-mysql-replication-metrics.timer <<'TIMER'
[Unit]
Description=Sample Yeodam staging MySQL replication metrics every 15 seconds

[Timer]
OnBootSec=30s
OnUnitActiveSec=15s
AccuracySec=1s
Persistent=true
Unit=yeodam-mysql-replication-metrics.service

[Install]
WantedBy=timers.target
TIMER

systemctl daemon-reload
systemctl start yeodam-mysql-replication-metrics.service
if [[ "$role" == source ]]; then
  cd "$directory"
  docker compose -f "$compose" config --quiet
  docker compose -f "$compose" run -T --rm alloy validate /etc/alloy/config.alloy </dev/null
  docker compose -f "$compose" up -d
else
  image='grafana/alloy:v1.20.1@sha256:2aa2099af76c0098d4af7a4d6e48f86cb66dc1a000222ad927a1c67c6542d13f'
  install -d -m 0755 /var/lib/yeodam/mysql-alloy
  docker run --rm --volume "$directory/$config:/etc/alloy/config.alloy:ro" \
    "$image" validate /etc/alloy/config.alloy
  cat > /etc/systemd/system/yeodam-mysql-alloy.service <<EOF
[Unit]
Description=Yeodam staging MySQL replication metrics Alloy
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Restart=always
RestartSec=10
ExecStartPre=-/usr/bin/docker rm -f yeodam-v2-mysql-alloy
ExecStart=/usr/bin/docker run --rm --name yeodam-v2-mysql-alloy --network host --user 0:0 --security-opt no-new-privileges:true --volume $directory/$config:/etc/alloy/config.alloy:ro --volume /var/lib/yeodam/mysql-metrics:/var/lib/yeodam/mysql-metrics:ro --volume /var/lib/yeodam/mysql-alloy:/var/lib/alloy/data $image run --storage.path=/var/lib/alloy/data --disable-reporting /etc/alloy/config.alloy
ExecStop=/usr/bin/docker stop yeodam-v2-mysql-alloy

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now yeodam-mysql-alloy.service
fi
systemctl enable --now yeodam-mysql-replication-metrics.timer
echo "MySQL $role metric sampler and Alloy are running."
