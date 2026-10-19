#!/usr/bin/env bash
set -Eeuo pipefail
cd /opt/yeodam-kafka
kafka() {
  docker compose --env-file /etc/yeodam/kafka/runtime.env exec -T --interactive=false \
    -e KAFKA_HEAP_OPTS='-Xms32m -Xmx256m' kafka \
    /opt/kafka/bin/kafka-topics.sh --bootstrap-server 127.0.0.1:9092 "$@"
}
config() {
  docker compose --env-file /etc/yeodam/kafka/runtime.env exec -T --interactive=false \
    -e KAFKA_HEAP_OPTS='-Xms32m -Xmx256m' kafka \
    /opt/kafka/bin/kafka-configs.sh --bootstrap-server 127.0.0.1:9092 \
    --entity-type topics --entity-name "$1" --alter --add-config "$2"
}
# Read existing topics once. Do not launch three JVMs per already-correct topic.
descriptions=$(kafka --describe)
ensure_topic() {
  local topic="$1" partitions="$2" policy="$3" retention="$4" timestamp="${5:-}" header current desired entry
  local -a create_args entries
  desired="cleanup.policy=$policy,retention.ms=$retention,segment.ms=86400000,min.insync.replicas=1"
  [[ -z "$timestamp" ]] || desired+=",message.timestamp.type=$timestamp"
  header=$(awk -v topic="$topic" '$1 == "Topic:" && $2 == topic && /PartitionCount:/ { print; exit }' <<< "$descriptions")
  if [[ -z "$header" ]]; then
    create_args=(--create --topic "$topic" --partitions "$partitions" --replication-factor 1)
    IFS=',' read -ra entries <<< "$desired"
    for entry in "${entries[@]}"; do create_args+=(--config "$entry"); done
    kafka "${create_args[@]}"
    return
  fi
  grep -Eq "PartitionCount:[[:space:]]*$partitions[[:space:]].*ReplicationFactor:[[:space:]]*1([[:space:]]|$)" <<< "$header" || {
    printf 'Unexpected existing partition count or replication factor: %s\n' "$topic" >&2; exit 1;
  }
  current=${header#*Configs:}
  current=${current//[[:space:]]/}
  IFS=',' read -ra entries <<< "$desired"
  for entry in "${entries[@]}"; do
    if [[ ",$current," != *",$entry,"* ]]; then config "$topic" "$desired"; break; fi
  done
}
for flow in trip-create trip-update story-create; do
  for direction in request result; do
    topic="prod.yeodam.$flow.$direction"
    timestamp=''
    [[ "$direction" != request ]] || timestamp=LogAppendTime
    ensure_topic "$topic" 2 delete 604800000 "$timestamp"
    ensure_topic "$topic-dlt" 2 delete 2592000000
  done
done
ensure_topic prod.connect-configs 1 compact -1
ensure_topic prod.connect-offsets 5 compact -1
ensure_topic prod.connect-status 1 compact -1
ensure_topic prod.yeodam.schema-history 1 delete -1
kafka --describe
