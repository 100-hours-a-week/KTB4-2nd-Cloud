FROM quay.io/strimzi/kafka@sha256:b9b9e37491ce17c1b2722b3668661de91430d9437acf18c5f5bafaef38138652
# The build context is prepared from checksum-verified, path-safe public artifacts.
# No credentials, runtime downloads, extra entrypoint, or unrelated connectors.
COPY --chown=1001:0 plugins/ /opt/kafka/plugins/debezium-mysql/
USER 1001
