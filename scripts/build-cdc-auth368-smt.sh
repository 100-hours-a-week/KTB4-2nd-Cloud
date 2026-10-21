#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || "$1" != /* ]]; then
  echo 'usage: build-cdc-auth368-smt.sh /absolute/private/output.jar' >&2
  exit 2
fi

root="$(cd "$(dirname "$0")/.." && pwd)"
scratch="$(mktemp -d)"
container=''
cleanup() {
  if [[ -n "$container" ]]; then docker rm "$container" >/dev/null 2>&1 || true; fi
  rm -rf "$scratch"
}
trap cleanup EXIT

image='quay.io/debezium/connect:3.4.3.Final@sha256:bad9e9cba46851b6d26cbf4c807630f709897b8b4b3e64fb5f2fbc26e1eec0b0'
container="$(docker create --platform linux/amd64 --entrypoint /bin/true "$image")"
for name in connect-api connect-json kafka-clients; do
  docker cp "$container:/kafka/libs/$name-4.1.1.jar" "$scratch/"
done
mkdir -p "$scratch/classes"
javac -cp "$scratch/*" -d "$scratch/classes" \
  "$root/java/cdc-auth368/src/main/java/com/yeodam/cdc/EncryptAuthRecord.java" \
  "$root/java/cdc-auth368/src/test/java/com/yeodam/cdc/EncryptAuthRecordSmoke.java"
docker run --rm --platform linux/amd64 --network none --entrypoint java \
  --mount "type=bind,source=$scratch/classes,target=/test,readonly" \
  "$image" -cp '/test:/kafka/libs/*' com.yeodam.cdc.EncryptAuthRecordSmoke
jar cf "$1" -C "$scratch/classes" com/yeodam/cdc/EncryptAuthRecord.class
echo "SMT JAR: $1"
