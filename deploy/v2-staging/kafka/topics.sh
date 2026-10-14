#!/usr/bin/env bash
set -Eeuo pipefail
cd /opt/yeodam-kafka
kafka() {
  docker compose --env-file /etc/yeodam/kafka/runtime.env exec -T --interactive=false kafka \
    /opt/kafka/bin/kafka-topics.sh --bootstrap-server 127.0.0.1:9092 "$@"
}
config() {
  docker compose --env-file /etc/yeodam/kafka/runtime.env exec -T --interactive=false kafka \
    /opt/kafka/bin/kafka-configs.sh --bootstrap-server 127.0.0.1:9092 \
    --entity-type topics --entity-name "$1" --alter --add-config "$2"
}
ensure_topic() {
  local topic="$1" partitions="$2" policy="$3" retention="$4" description
  kafka --create --if-not-exists --topic "$topic" --partitions "$partitions" --replication-factor 1 \
    --config "cleanup.policy=$policy" --config "retention.ms=$retention" \
    --config segment.ms=86400000 --config min.insync.replicas=1
  description=$(kafka --describe --topic "$topic")
  if ! grep -Eq "PartitionCount:[[:space:]]*$partitions[[:space:]].*ReplicationFactor:[[:space:]]*1([[:space:]]|$)" <<< "$description"; then
    printf 'Unexpected existing partition count or replication factor: %s\n' "$topic" >&2
    exit 1
  fi
  config "$topic" "cleanup.policy=$policy,retention.ms=$retention,segment.ms=86400000,min.insync.replicas=1"
  printf '%s\n' "$description"
}
for flow in trip-create trip-update story-create; do
  for direction in request result; do
    topic="staging.yeodam.$flow.$direction"
    ensure_topic "$topic" 2 delete 604800000
    ensure_topic "$topic-dlt" 2 delete 2592000000
  done
done
ensure_topic staging.connect-configs 1 compact -1
ensure_topic staging.connect-offsets 5 compact -1
ensure_topic staging.connect-status 1 compact -1
ensure_topic staging.yeodam.schema-history 1 delete -1
