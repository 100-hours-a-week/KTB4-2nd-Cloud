#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'Run as root on the staging Kafka host' >&2; exit 1; }
cd /opt/yeodam-kafka
[[ -s compose.yml && -s /etc/yeodam/kafka/runtime.env ]] || exit 1
if [[ -f compose.override.yml ]] && ! grep -q '^# yeodam-kafka-observability$' compose.override.yml; then
  echo 'Unrelated Compose override requires review' >&2; exit 1
fi
# No connector configuration or offset data is read into logs or modified.
curl -fsS http://127.0.0.1:8083/connectors/yeodam-staging-outbox/status | python3 -c '
import json,sys
s=json.load(sys.stdin)
assert s["connector"]["state"]=="RUNNING" and s["tasks"] and all(t["state"]=="RUNNING" for t in s["tasks"])
print("Before: connector and tasks RUNNING")'
broker=$(docker compose --env-file /etc/yeodam/kafka/runtime.env ps -q kafka)
broker_started=$(docker inspect --format '{{.State.StartedAt}}' "$broker")
install -d -m 0755 metrics
if ! printf '%s  %s\n' 0ad843261c567d1924a017d542e14c40f2afff523d940c25ee000742fde4b3a9 metrics/jmx-agent.jar | sha256sum -c --status; then
  curl --connect-timeout 10 --max-time 90 --retry 2 -fsSL \
    https://github.com/prometheus/jmx_exporter/releases/download/1.7.0/jmx_prometheus_javaagent-1.7.0.jar \
    -o metrics/jmx-agent.jar.next
  printf '%s  %s\n' 0ad843261c567d1924a017d542e14c40f2afff523d940c25ee000742fde4b3a9 metrics/jmx-agent.jar.next | sha256sum -c
  chmod 0644 metrics/jmx-agent.jar.next
  mv metrics/jmx-agent.jar.next metrics/jmx-agent.jar
fi
backup=$(mktemp -d)
[[ ! -f metrics/connect-jmx.yml ]] || cp metrics/connect-jmx.yml "$backup/jmx"
printf '%s' "$JMX_CONFIG_B64" | base64 -d > metrics/connect-jmx.yml
chmod 0644 metrics/connect-jmx.yml
[[ ! -f compose.override.yml ]] || cp compose.override.yml "$backup/override"
printf '%s' "$METRICS_COMPOSE_B64" | base64 -d > compose.override.yml
compose() { docker compose --env-file /etc/yeodam/kafka/runtime.env "$@"; }
success=false
cleanup() {
  if [[ "$success" != true ]]; then
    echo 'Restoring previous Connect configuration after install failure' >&2
    [[ ! -f "$backup/jmx" ]] || cp "$backup/jmx" metrics/connect-jmx.yml
    compose stop kafka-exporter || true
    if [[ -f "$backup/override" ]]; then cp "$backup/override" compose.override.yml; else rm -f compose.override.yml; fi
    compose up -d --no-deps connect || true
  fi
  rm -rf "$backup"
}
trap cleanup EXIT
compose config --quiet
compose pull kafka-exporter
compose up -d --no-deps connect kafka-exporter
for attempt in $(seq 1 36); do
  if curl -fsS http://127.0.0.1:9404/metrics >/dev/null && curl -fsS http://127.0.0.1:9308/metrics >/dev/null &&
     curl -fsS http://127.0.0.1:8083/connectors/yeodam-staging-outbox/status | python3 -c '
import json,sys
s=json.load(sys.stdin)
assert s["connector"]["state"]=="RUNNING" and s["tasks"] and all(t["state"]=="RUNNING" for t in s["tasks"])' >/dev/null; then
    [[ "$broker" == "$(compose ps -q kafka)" ]] || { echo 'Broker container changed unexpectedly' >&2; exit 1; }
    [[ "$broker_started" == "$(docker inspect --format '{{.State.StartedAt}}' "$broker")" ]] || { echo 'Broker restarted unexpectedly' >&2; exit 1; }
    success=true
    echo 'Kafka broker unchanged; Connect RUNNING; both metrics endpoints ready'
    exit 0
  fi
  sleep 5
done
echo 'Connect metrics did not become ready' >&2
exit 1
