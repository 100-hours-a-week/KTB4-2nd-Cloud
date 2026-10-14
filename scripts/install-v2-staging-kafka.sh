#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'Run as root on the staging Kafka EC2' >&2; exit 1; }
mountpoint -q /data/kafka || { echo 'Kafka data EBS is not mounted' >&2; exit 1; }
[[ -f /var/lib/yeodam-kafka-host-ready ]] || { echo 'Kafka host initialization is incomplete' >&2; exit 1; }
[[ -n "${KAFKA_COMPOSE_B64:-}" && -n "${KAFKA_TOPICS_B64:-}" ]] || { echo 'Kafka release files are missing' >&2; exit 1; }
install -d -m 0750 /opt/yeodam-kafka
install -d -m 0700 /etc/yeodam/kafka
printf '%s' "$KAFKA_COMPOSE_B64" | base64 --decode > /opt/yeodam-kafka/compose.yml
printf '%s' "$KAFKA_TOPICS_B64" | base64 --decode > /opt/yeodam-kafka/topics.sh
chmod 0750 /opt/yeodam-kafka/topics.sh
cd /opt/yeodam-kafka
# apache/kafka's appuser is UID 1000. Do not recursively rewrite stored files.
# ext4 recovery directory is not a Kafka topic directory. Never delete contents.
if [[ -d /data/kafka/lost+found ]]; then
  rmdir /data/kafka/lost+found || { echo 'Non-empty lost+found requires manual recovery' >&2; exit 1; }
fi
chown 1000:1000 /data/kafka
umask 077
if [[ ! -s /data/kafka/.cluster-id ]]; then
  if [[ -e /data/kafka/meta.properties ]] || find /data/kafka -mindepth 1 -maxdepth 1 -type d ! -name lost+found | grep -q .; then
    echo 'Existing Kafka data requires recovery of its original cluster ID' >&2
    exit 1
  fi
  python3 - <<'PY' > /data/kafka/.cluster-id
import base64, uuid
print(base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip('='))
PY
fi
cluster_id=$(cat /data/kafka/.cluster-id)
[[ "$cluster_id" =~ ^[A-Za-z0-9_-]{22}$ ]] || { echo 'Invalid persisted KRaft cluster ID' >&2; exit 1; }
if [[ -f /data/kafka/meta.properties ]]; then
  grep -Fxq "cluster.id=$cluster_id" /data/kafka/meta.properties || { echo 'Persisted cluster ID mismatch' >&2; exit 1; }
fi
printf 'KAFKA_CLUSTER_ID=%s\n' "$cluster_id" > /etc/yeodam/kafka/runtime.env
cat > /etc/systemd/system/yeodam-kafka.service <<'UNIT'
[Unit]
Description=Yeodam staging Kafka and Connect
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target
RequiresMountsFor=/data/kafka
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/yeodam-kafka
ExecStartPre=/usr/bin/mountpoint -q /data/kafka
ExecStart=/usr/bin/docker compose --env-file /etc/yeodam/kafka/runtime.env up -d
ExecStop=/usr/bin/docker compose --env-file /etc/yeodam/kafka/runtime.env stop
TimeoutStartSec=600
TimeoutStopSec=150
[Install]
WantedBy=multi-user.target
UNIT
docker compose --env-file /etc/yeodam/kafka/runtime.env config --quiet
docker compose --env-file /etc/yeodam/kafka/runtime.env pull
docker compose --env-file /etc/yeodam/kafka/runtime.env up -d kafka
for attempt in $(seq 1 60); do
  if docker compose --env-file /etc/yeodam/kafka/runtime.env exec -T --interactive=false kafka \
    /opt/kafka/bin/kafka-topics.sh --bootstrap-server 127.0.0.1:9092 --list >/dev/null 2>&1; then
    break
  fi
  [[ "$attempt" != 60 ]] || { docker compose --env-file /etc/yeodam/kafka/runtime.env logs --tail 40 kafka; exit 1; }
  sleep 5
done
bash /opt/yeodam-kafka/topics.sh
docker compose --env-file /etc/yeodam/kafka/runtime.env up -d connect
for attempt in $(seq 1 60); do
  if curl --fail --silent http://127.0.0.1:8083/connector-plugins | grep -q 'io.debezium.connector.mysql.MySqlConnector'; then
    systemctl daemon-reload
    systemctl enable yeodam-kafka.service
    systemctl start yeodam-kafka.service
    docker compose --env-file /etc/yeodam/kafka/runtime.env ps
    echo 'Kafka and Connect are ready; no application connector has been registered'
    exit 0
  fi
  sleep 5
done
docker compose --env-file /etc/yeodam/kafka/runtime.env logs --tail 40 connect
exit 1
