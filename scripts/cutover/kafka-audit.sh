set -Eeuo pipefail
umask 077
trap 'rm -f /tmp/audit.properties' EXIT
cat > /tmp/audit.properties <<EOF
security.protocol=SASL_SSL
sasl.mechanism=SCRAM-SHA-512
sasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required username="v3-cutover-audit" password="$(cat /credentials/password)";
ssl.truststore.type=PEM
ssl.truststore.location=/ca/ca.crt
ssl.endpoint.identification.algorithm=https
request.timeout.ms=5000
default.api.timeout.ms=20000
EOF
export KAFKA_HEAP_OPTS='-Xms32m -Xmx96m'
bootstrap=yeodam-v3-kafka-kafka-bootstrap.kafka.svc:9093
report=/tmp/group-report
if /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server "$bootstrap" --command-config /tmp/audit.properties --describe --group "$1" > "$report" 2>&1; then
  if grep -q "^$1 " "$report"; then cat "$report"; exit 0; fi
else
  grep -q 'does not exist' "$report" || exit 1
fi
# An unused group is safe only if each of its actual input topics is empty.
# Offsets can be nonzero after retention; compare earliest and latest per partition.
shift
for topic in "$@"; do
  /opt/kafka/bin/kafka-get-offsets.sh --bootstrap-server "$bootstrap" --command-config /tmp/audit.properties --topic "$topic" --time -2 | sort > /tmp/begin
  /opt/kafka/bin/kafka-get-offsets.sh --bootstrap-server "$bootstrap" --command-config /tmp/audit.properties --topic "$topic" --time -1 | sort > /tmp/end
  test -s /tmp/begin && cmp -s /tmp/begin /tmp/end || exit 1
done
printf 'EMPTY_GROUP_VERIFIED\n'
