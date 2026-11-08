set -Eeuo pipefail
umask 077
bootstrap=yeodam-v3-kafka-kafka-bootstrap.kafka.svc:9093
tool=/opt/kafka/bin
token="v3-access-$(date +%s)-$RANDOM"
trap 'rm -f /tmp/writer.properties /tmp/reader.properties /tmp/denied.properties /tmp/wrong.properties /tmp/denial.log /tmp/result /tmp/description /tmp/produce.log' EXIT
for role in writer reader denied; do
  cat > "/tmp/$role.properties" <<EOF
security.protocol=SASL_SSL
sasl.mechanism=SCRAM-SHA-512
sasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required username="v3-access-$role" password="$(cat "/credentials/$role/password")";
ssl.truststore.type=PEM
ssl.truststore.location=/ca/ca.crt
ssl.endpoint.identification.algorithm=https
request.timeout.ms=5000
default.api.timeout.ms=20000
EOF
done
# Wait for Operator topic/ACL reconciliation, without leaking authentication errors.
ready=false
for attempt in {1..12}; do
  if "$tool/kafka-topics.sh" --bootstrap-server "$bootstrap" --command-config /tmp/writer.properties \
      --describe --topic v3-access-check > /tmp/description 2>/dev/null; then ready=true;break;fi
  sleep 5
done
[[ "$ready" == true ]] || { echo 'Topic/ACL readiness timed out'; exit 1; }
# One partition, three replicas, and all replicas in ISR must be visible.
awk '
/Partition: / {
  for (i=1; i<=NF; i++) {
    if ($i == "Replicas:") {n=split($(i+1), a, ","); if(n!=3)exit 1;}
    if ($i == "Isr:") {n=split($(i+1), a, ","); if(n!=3)exit 1;seen++;}
  }
}
END {if(seen!=1)exit 1;}' /tmp/description
produce() {
  printf '%s\n' "$token" | "$tool/kafka-console-producer.sh" --bootstrap-server "$bootstrap" \
    --topic "$2" --producer.config "/tmp/$1.properties" --producer-property acks=all \
    --producer-property enable.idempotence=true --producer-property max.block.ms=15000 \
    --producer-property delivery.timeout.ms=15000 > /tmp/produce.log 2>&1
}
consume() {
  "$tool/kafka-console-consumer.sh" --bootstrap-server "$bootstrap" --topic v3-access-check \
    --consumer.config "/tmp/$1.properties" --group "$2" --from-beginning --max-messages 1 --timeout-ms 15000
}
produce writer v3-access-check
consume reader v3-access-check-reader > /tmp/result 2>/dev/null
grep -Fx "$token" /tmp/result >/dev/null
# The denial marker is required; timeout/DNS/TLS failures must never count as ACL success.
expect_denied() {
  local pattern="$1"; shift
  # Kafka ConsoleConsumer may log poll-time authorization failures and return 0.
  # The typed broker error, rather than the CLI status, proves the rejection.
  "$@" > /tmp/denial.log 2>&1 || true
  grep -Eq "$pattern" /tmp/denial.log || { echo 'Failure was not the expected authorization rejection';exit 1; }
}
# Console producer writes errors to its own captured file; test it separately.
produce denied v3-access-check || true
# With no Topic Write ACL anywhere, InitProducerId rejects idempotent writes at cluster level.
# This is an expected denial; do not add Cluster IdempotentWrite just for the test.
grep -Eq 'ClusterAuthorizationException|CLUSTER_AUTHORIZATION_FAILED|TopicAuthorizationException|TOPIC_AUTHORIZATION_FAILED' /tmp/produce.log || { echo 'Denied writer unexpectedly published or failed for another reason';exit 1; }
produce writer v3-access-other || true
grep -Eq 'TopicAuthorizationException|TOPIC_AUTHORIZATION_FAILED' /tmp/produce.log || { echo 'Unlisted Topic was not rejected';exit 1; }
expect_denied 'TopicAuthorizationException|TOPIC_AUTHORIZATION_FAILED' consume denied v3-access-check-denied
expect_denied 'GroupAuthorizationException|GROUP_AUTHORIZATION_FAILED' consume reader v3-access-wrong-group
sed 's/password="[^"]*"/password="definitely-incorrect"/' /tmp/writer.properties > /tmp/wrong.properties
produce wrong v3-access-check || true
grep -Eq 'SaslAuthenticationException|SASL_AUTHENTICATION_FAILED|Authentication failed' /tmp/produce.log || { echo 'Wrong credentials were not rejected';exit 1; }
# Clean committed offsets from these two disposable groups before removing users.
for role in reader denied; do
  if ! "$tool/kafka-consumer-groups.sh" --bootstrap-server "$bootstrap" --command-config "/tmp/$role.properties" \
      --delete --group "v3-access-check-$role" > /tmp/denial.log 2>&1; then
    grep -Eq 'GROUP_ID_NOT_FOUND|GroupIdNotFound|does not exist' /tmp/denial.log || { echo 'Test group cleanup failed';exit 1; }
  fi
done
echo 'PASS: TLS/SCRAM, acks=all token round-trip, RF3/ISR3, forbidden write/read/Topic/Group and invalid-password rejection'
