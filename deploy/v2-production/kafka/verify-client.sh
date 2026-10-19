#!/usr/bin/env bash
set -Eeuo pipefail
[[ "${KAFKA_TEST_RUN_ID:-}" =~ ^[0-9a-f]{32}$ ]] || { echo 'Invalid test run ID' >&2; exit 1; }
[[ "${KAFKA_CLIENT_ROLE:-}" =~ ^(backend|ai)$ ]] || { echo 'Invalid client role' >&2; exit 1; }

server=kafka.production.yeodam.internal:9092
topic="prod.cloud.preflight.$KAFKA_TEST_RUN_ID"
message="yeodam-kafka-preflight-$KAFKA_TEST_RUN_ID"
bin=/opt/kafka/bin
export KAFKA_HEAP_OPTS='-Xms128m -Xmx256m'

# Test only a unique disposable topic; never read/reset a business consumer group.
"$bin/kafka-topics.sh" --bootstrap-server "$server" --create --topic "$topic" \
  --partitions 1 --replication-factor 1 --config retention.ms=60000
trap '"$bin/kafka-topics.sh" --bootstrap-server "$server" --delete --topic "$topic" >/dev/null' EXIT
printf '%s\n' "$message" | "$bin/kafka-console-producer.sh" \
  --bootstrap-server "$server" --topic "$topic" --producer-property acks=all
observed=$("$bin/kafka-console-consumer.sh" --bootstrap-server "$server" --topic "$topic" \
  --partition 0 --offset earliest --max-messages 1 --timeout-ms 15000)
[[ "$observed" == "$message" ]] || { echo 'Probe message mismatch' >&2; exit 1; }
printf '{"client":"%s","metadataProduceConsume":"passed"}\n' "$KAFKA_CLIENT_ROLE"
