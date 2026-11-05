#!/bin/bash
set -Eeuo pipefail
umask 077
[[ $# == 0 ]] || exit 1
cd /schema/sql
sha256sum -c /schema/checksums.sha256 > /dev/null
password=$(cat /credentials/app-password)
[[ $password =~ ^[a-f0-9]{48}$ ]] || exit 1
trap 'rm -f /tmp/flyway-controlled.conf /tmp/flyway.log /tmp/mysql-trust.p12' EXIT
keytool -importcert -noprompt -alias mysql-ca -file /tls/ca.crt -keystore /tmp/mysql-trust.p12 -storetype PKCS12 -storepass public-ca-only -J-Xmx64m > /dev/null 2>&1
cat > /tmp/flyway-controlled.conf <<EOF
flyway.url=jdbc:mysql://mysql-primary.mysql.svc.cluster.local:3306/yeodam?sslMode=VERIFY_IDENTITY&trustCertificateKeyStoreUrl=file:/tmp/mysql-trust.p12&trustCertificateKeyStoreType=PKCS12&trustCertificateKeyStorePassword=public-ca-only&fallbackToSystemTrustStore=false&allowPublicKeyRetrieval=false&connectTimeout=5000&socketTimeout=60000
flyway.driver=com.mysql.cj.jdbc.Driver
flyway.user=yeodam
flyway.password=$password
flyway.locations=filesystem:/schema/sql
flyway.defaultSchema=yeodam
flyway.schemas=yeodam
flyway.createSchemas=false
flyway.target=34
flyway.cleanDisabled=true
flyway.baselineOnMigrate=false
flyway.outOfOrder=false
flyway.validateOnMigrate=true
flyway.validateMigrationNaming=true
flyway.failOnMissingLocations=true
flyway.ignoreMigrationPatterns=
flyway.connectRetries=0
EOF
unset password
if ! env -i PATH="$PATH" HOME=/tmp REDGATE_DISABLE_TELEMETRY=true JAVA_ARGS='-Xms32m -Xmx128m -XX:ActiveProcessorCount=1' /flyway/flyway -configFiles=/tmp/flyway-controlled.conf migrate > /tmp/flyway.log 2>&1; then
  echo 'FAIL: Flyway schema migration rejected; no baseline, clean or repair attempted' >&2
  exit 1
fi
echo 'PASS: pinned BE Flyway migrations applied/validated through V34 using TLS identity verification'
