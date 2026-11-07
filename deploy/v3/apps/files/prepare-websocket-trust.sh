#!/bin/sh
set -eu
# Public certificates only. Preserve the exact image's public internet roots.
keytool -importkeystore -noprompt -srckeystore "$JAVA_HOME/lib/security/cacerts" -srcstorepass changeit -destkeystore /etc/yeodam/trust/cacerts -deststoretype JKS -deststorepass public-ca-only >/dev/null 2>&1
for ca in redis; do
  ! grep -q 'PRIVATE KEY' "/etc/yeodam/ca/$ca.crt"
  keytool -importcert -noprompt -alias "yeodam-v3-$ca" -file "/etc/yeodam/ca/$ca.crt" -keystore /etc/yeodam/trust/cacerts -storepass public-ca-only >/dev/null 2>&1
done
chmod 0444 /etc/yeodam/trust/cacerts
