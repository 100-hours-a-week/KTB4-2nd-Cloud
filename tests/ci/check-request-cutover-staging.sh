#!/usr/bin/env bash
set -Eeuo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
image="${1:?existing request-cutover test image required}"
fixture="cutover-stage-fixture-${RANDOM}-$$"
folder=$(mktemp -d)
cleanup() {
  docker rm -f "$fixture" >/dev/null 2>&1 || true
  rm -rf -- "$folder"
}
trap cleanup EXIT
PYTHONPATH="$root/scripts" python3 -m request_cutover.staging render \
  --plan "$root/deploy/request-cutover/staging/plan.example.json" --release 1111111111111111111111111111111111111111 \
  --output "$folder/bundle"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=v1-staging.yeodam-2gether.com' \
  -addext 'subjectAltName=DNS:v1-staging.yeodam-2gether.com' \
  -keyout "$folder/key.pem" -out "$folder/cert.pem" 2>/dev/null
python3 - "$folder" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); s=(p/'bundle/nginx.candidate.conf').read_text()
s=s.replace('/etc/letsencrypt/live/v1-staging.yeodam-2gether.com/fullchain.pem','/fixture/cert.pem')
s=s.replace('/etc/letsencrypt/live/v1-staging.yeodam-2gether.com/privkey.pem','/fixture/key.pem')
(p/'nginx.conf').write_text('events {}\nhttp {\n'+s+'\n}\n')
PY
CUTOVER_GATE_IMAGE=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
CUTOVER_CONFIG_DIR="$folder/bundle" CUTOVER_STATE_DIR="$folder/state" \
  docker compose -f "$folder/bundle/compose.yml" config --quiet
docker run -d --name "$fixture" --network none -e PYTHONPATH=/work/scripts \
  -v "$root:/work:ro" -v "$folder:/fixture:ro" --entrypoint python "$image" \
  /work/tests/ci/request-cutover-staging-fixture.py --config /fixture/bundle/gate.json >/dev/null
ready=false
for _ in {1..30}; do
  if docker exec "$fixture" python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:19091/api/actuator/health",timeout=1)' 2>/dev/null; then ready=true; break; fi
  sleep 0.2
done
[[ "$ready" == true ]]
docker run --rm --network "container:$fixture" --entrypoint sh \
  -v "$folder:/fixture:ro" nginx:1.24.0-alpine -ec '
    nginx -t -c /fixture/nginx.conf
    nginx -c /fixture/nginx.conf
    trap "nginx -s quit -c /fixture/nginx.conf" EXIT
    host=v1-staging.yeodam-2gether.com
    curl --noproxy "*" --cacert /fixture/cert.pem --resolve "$host:443:127.0.0.1" \
      --max-time 5 --fail -sS -D /tmp/headers -o /tmp/body \
      -H "Cookie: fixture=only-local" -H "X-CSRF-TOKEN: local-csrf" \
      -H "X-Request-ID: 11111111-1111-4111-a111-111111111111_42" \
      --data-binary "unchanged+body" "https://$host/api/trips?cursor=a%2Fb&probe=local"
    test "$(cat /tmp/body)" = "unchanged+body"
    tr -d "\r" </tmp/headers >/tmp/clean
    grep -Fiqx "X-Request-ID: 11111111-1111-4111-a111-111111111111_42" /tmp/clean
    grep -Fiqx "X-Mock-URI: /api/trips?cursor=a%2Fb&probe=local" /tmp/clean
    grep -Fiqx "X-Mock-Host: $host" /tmp/clean
    grep -Fiqx "X-Mock-CSRF: local-csrf" /tmp/clean
    grep -Fiqx "X-Mock-Cookie: fixture=only-local" /tmp/clean
    grep -Fiqx "X-Mock-Backend: api" /tmp/clean
    test "$(grep -ic "^Set-Cookie:" /tmp/clean)" = 2
    curl --noproxy "*" --cacert /fixture/cert.pem --resolve "$host:443:127.0.0.1" \
      --max-time 5 --fail -sS -D /tmp/web -o /dev/null "https://$host/_next/local"
    grep -Fiq "X-Mock-Backend: web" /tmp/web
    code=$(curl --noproxy "*" --cacert /fixture/cert.pem --resolve "$host:443:127.0.0.1" \
      --max-time 5 -sS -o /dev/null -w "%{http_code}" -H "Host: wrong.example" "https://$host/")
    test "$code" = 421
  '
echo 'staging Nginx 1.24 -> preparation gate -> V1 protocol: passed'
