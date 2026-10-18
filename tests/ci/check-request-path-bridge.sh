#!/usr/bin/env bash
# Local fixture only: ephemeral TLS files and a container with no external network.
set -Eeuo pipefail

cd "$(dirname "$0")/../.."
nginx_image='nginx@sha256:ec4ed8b5299e5e90694af7750eb6dffd2627317d30544d056b0371f8082f7bce'
bridge_check_dir="$(mktemp -d)"
trap 'rm -rf -- "${bridge_check_dir}"' EXIT

openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj '/CN=cutover-staging.yeodam-2gether.com' \
  -addext 'subjectAltName=DNS:cutover-staging.yeodam-2gether.com,DNS:staging.yeodam-2gether.com' \
  -keyout "${bridge_check_dir}/key.pem" -out "${bridge_check_dir}/cert.pem" 2>/dev/null
python3 - "${bridge_check_dir}" <<'PY'
import json
from pathlib import Path
import sys

folder = Path(sys.argv[1])
config = json.loads(Path('deploy/request-path-bridge/config.example.json').read_text())
config.update(certificate='/work/cert.pem', certificate_key='/work/key.pem',
              upstream_ca_bundle='/work/cert.pem')
(folder / 'config.json').write_text(json.dumps(config))
# A named local upstream bypasses public DNS only in this test fixture.
# It exercises the generated proxy_pass/TLS/header handling, not ALB discovery.
fixture = '''
upstream example-staging-123.ap-northeast-2.elb.amazonaws.com {
    server 127.0.0.1:8443;
}
server {
    listen 127.0.0.1:8443 ssl;
    ssl_certificate /work/cert.pem;
    ssl_certificate_key /work/key.pem;
    add_header X-Mock-Host $http_host always;
    add_header X-Mock-URI $request_uri always;
    add_header X-Mock-Method $request_method always;
    add_header X-Mock-Proto $http_x_forwarded_proto always;
    add_header X-Mock-For $http_x_forwarded_for always;
    add_header X-Mock-SNI $ssl_server_name always;
    add_header X-Mock-Cookie $http_cookie always;
    add_header X-Mock-Authorization $http_authorization always;
    location = /slow { alias /work/payload; limit_rate 16k; }
    location / { return 201 'local fixture only'; }
}
'''
(folder / 'payload').write_bytes(b'x' * 32768)
(folder / 'nginx.conf').write_text('events {}\nhttp { include /work/bridge.conf;\n' + fixture + '}\n')
PY
python3 scripts/render-request-path-bridge.py render \
  --config "${bridge_check_dir}/config.json" \
  --release 1111111111111111111111111111111111111111 \
  --output "${bridge_check_dir}/bridge.conf" --allow-unverified
docker run --rm --network none --entrypoint nginx \
  -v "${bridge_check_dir}:/work:ro" "${nginx_image}" -t -c /work/nginx.conf
docker run --rm --network none --entrypoint sh \
  -v "${bridge_check_dir}:/work:ro" "${nginx_image}" -ec '
    nginx -c /work/nginx.conf
    trap "nginx -s quit -c /work/nginx.conf" EXIT
    origin=https://cutover-staging.yeodam-2gether.com
    # No host port is published. All requests stay inside this container.
    curl --noproxy "*" --cacert /work/cert.pem \
      --resolve cutover-staging.yeodam-2gether.com:443:127.0.0.1 \
      --max-time 5 -sS -D /tmp/headers -o /dev/null \
      -H "X-Forwarded-For: 198.51.100.1" -H "X-Forwarded-Proto: http" \
      -H "Cookie: fixture=local-only" -H "Authorization: Bearer local-only" \
      --data-binary "local-body" "$origin/api/trips?cursor=a%2Fb&probe=local"
    tr -d "\r" < /tmp/headers > /tmp/clean-headers
    grep -Fqx "HTTP/1.1 201 Created" /tmp/clean-headers
    grep -Fiqx "X-Mock-Host: cutover-staging.yeodam-2gether.com" /tmp/clean-headers
    grep -Fiqx "X-Mock-URI: /api/trips?cursor=a%2Fb&probe=local" /tmp/clean-headers
    grep -Fiqx "X-Mock-Method: POST" /tmp/clean-headers
    grep -Fiqx "X-Mock-Proto: https" /tmp/clean-headers
    grep -Fiqx "X-Mock-For: 127.0.0.1" /tmp/clean-headers
    grep -Fiqx "X-Mock-SNI: staging.yeodam-2gether.com" /tmp/clean-headers
    grep -Fiqx "X-Mock-Cookie: fixture=local-only" /tmp/clean-headers
    grep -Fiqx "X-Mock-Authorization: Bearer local-only" /tmp/clean-headers
    code=$(curl --noproxy "*" --cacert /work/cert.pem \
      --resolve cutover-staging.yeodam-2gether.com:443:127.0.0.1 \
      --max-time 5 -sS -o /dev/null -w "%{http_code}" -H "Host: wrong.example" "$origin/")
    test "$code" = 421
    # Start a slow response, wait for partial bytes, reload, then compare all bytes.
    curl --noproxy "*" --cacert /work/cert.pem \
      --resolve cutover-staging.yeodam-2gether.com:443:127.0.0.1 \
      --max-time 10 --fail -sSN -o /tmp/drained "$origin/slow" &
    request_pid=$!
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
      test -s /tmp/drained && break
      sleep 0.1
    done
    test -s /tmp/drained
    test "$(wc -c < /tmp/drained)" -lt 32768
    nginx -s reload -c /work/nginx.conf
    wait "$request_pid"
    cmp /work/payload /tmp/drained
    printf "Local TLS/Host/URI/header and in-flight reload fixture passed.\n"
  '
printf 'Real ALB DNS/TLS, application writes/jobs, session continuity and production Nginx remain unverified.\n'
