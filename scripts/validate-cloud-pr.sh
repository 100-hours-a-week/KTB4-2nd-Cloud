#!/usr/bin/env bash
set -Eeuo pipefail

bash -n scripts/*.sh tests/ci/*.sh
bash scripts/validate-image-versions.sh deploy/image-versions.env
python3 -m json.tool monitoring/cloudwatch-agent/app.json >/dev/null

temporary_dir="$(mktemp -d)"
trap 'rm -rf -- "${temporary_dir}"' EXIT
temporary_env="${temporary_dir}/ci.env"
cat > "${temporary_env}" <<'EOF'
MYSQL_URL=jdbc:mysql://host.docker.internal:3306/yeodam
MYSQL_USERNAME=ci
MYSQL_PASSWORD=ci-only
ATTACHMENT_S3_BUCKET=ci-only
AI_SERVER_BASE_URL=http://127.0.0.1:8000
AI_SERVER_API_KEY=ci-only
AI_EC2_INSTANCE_ID=i-ci-only
FRONTEND_OAUTH_CALLBACK_URI=http://localhost/auth/callback
KAKAO_CLIENT_ID=ci-only
KAKAO_REDIRECT_URI=http://localhost/api/auth/kakao/callback
KAKAO_CLIENT_SECRET=ci-only
JWT_SECRET=ci-only-secret-key-with-at-least-32-bytes
JWT_ISSUER=http://localhost
FRONTEND_ORIGIN=http://localhost
DATA_GO_KR_LEGAL_DISTRICT_API_URL=http://localhost/mock-places
API_KEY=ci-only
S3_BUCKET=ci-only
EOF

docker compose --env-file deploy/image-versions.env --env-file "${temporary_env}" \
  -f app-compose.yml config --format json > "${temporary_dir}/app.json"
docker compose --env-file deploy/image-versions.env --env-file "${temporary_env}" \
  -f worker-compose.yml config --format json > "${temporary_dir}/worker.json"

python3 - "${temporary_dir}" <<'PY'
import json
import pathlib
import sys

directory = pathlib.Path(sys.argv[1])
with (directory / 'app.json').open(encoding='utf-8') as stream:
    app = json.load(stream)['services']
with (directory / 'worker.json').open(encoding='utf-8') as stream:
    worker = json.load(stream)['services']

expected = {
    'frontend': ('yeodam-frontend', app),
    'backend': ('yeodam-backend', app),
    'ai-worker': ('yeodam-ai-worker', worker),
}
for service_name, (package, services) in expected.items():
    service = services[service_name]
    image = service['image']
    assert image.startswith(f'ghcr.io/100-hours-a-week/{package}:sha-'), image
    assert '@sha256:' in image, image
    assert service['platform'] == 'linux/amd64', service_name
    assert service.get('healthcheck'), service_name
    assert service.get('security_opt') == ['no-new-privileges:true'], service_name
    assert service.get('restart') == 'unless-stopped', service_name

assert 'mysql' not in app and 'nginx' not in app
assert 'mysql' not in worker and 'nginx' not in worker
for name in ('frontend', 'backend'):
    assert all(port.get('host_ip') == '127.0.0.1' for port in app[name]['ports'])
assert app['backend']['logging']['driver'] == 'awslogs'
assert worker['ai-worker']['logging']['driver'] == 'awslogs'
print('운영 Compose 배치와 Image 참조 검증 완료')
PY
