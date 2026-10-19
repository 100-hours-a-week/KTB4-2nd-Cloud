#!/usr/bin/env bash
set -Eeuo pipefail

python3 -m unittest discover -s tests/ci -p 'test_cloud_pr_scope.py'
python3 -m unittest discover -s tests/ci -p 'test_production_mysql_account.py'
bash -n terraform/v2/production/files/provision-mysql-app-account.sh
python3 -c 'import ast,pathlib; [ast.parse(pathlib.Path(p).read_text()) for p in ["scripts/provision-production-mysql-credentials.py", "terraform/v2/production/files/verify-storage.py"]]'
python3 -m unittest discover -s tests/ci -p 'test_ci_baseline.py'
python3 -m unittest discover -s tests/ci -p 'test_request_path_bridge.py'
bash tests/ci/check-request-path-bridge.sh
bash tests/ci/check-request-cutover.sh
node --check tests/load/scenarios/request-auth-cutover.js

bash -n scripts/*.sh tests/ci/*.sh
bash -n deploy/v2-staging/kafka/topics.sh
KAFKA_CLUSTER_ID=abcdefghijklmnopqrstuv docker compose -f deploy/v2-staging/kafka/compose.yml config --quiet
python3 -m unittest discover -s tests/load/tests -p 'test_synthetic_session_pool.py'
python3 -m unittest discover -s tests/load/tests -p 'test_migration_probe.py'
python3 -m unittest discover -s tests/load/tests -p 'test_migration_trip.py'
python3 -m unittest discover -s tests/load/tests -p 'test_cdc_322_contract.py'
python3 -m unittest discover -s tests/load/tests -p 'test_cdc_323_contract.py'
python3 -m unittest discover -s tests/load/tests -p 'test_cdc_323_ordered_audit.py'
python3 -m unittest discover -s tests/load/tests -p 'test_cdc_324_replay_contract.py'
python3 -m unittest discover -s tests/load/tests -p 'test_nginx_defense.py'
node --test tests/load/tests/nginx-defense.test.mjs
node --check tests/load/scenarios/nginx-request-repeat.js
node --test tests/load/tests/migration-probe.test.mjs
node --check tests/load/scenarios/migration-write.js
node --check tests/load/scenarios/migration-trip-create.js
bash -n tests/load/scripts/run-k6.sh
python3 -m unittest discover -s tests/load/tests -p 'test_migration_observability.py'
python3 -m unittest discover -s tests/load/tests -p 'test_staging_cutover.py'
python3 -m unittest discover -s tests/load/tests -p 'test_rehearsal_reset.py'
python3 -m unittest discover -s tests/load/tests -p 'test_migration_seed.py'
bash -n terraform/v2/staging/files/initialize-mysql-replica.sh
bash -n scripts/provision-staging-cutover-account.sh
bash -n monitoring/v2-staging/install-proxysql-metrics.sh
bash -n monitoring/v2-staging/prepare-ai-metrics-secret.sh
bash -n monitoring/v2-staging/install-kafka-observability.sh
bash -n monitoring/v2-staging/install-mysql-validation-metrics.sh
bash -n monitoring/v2-staging/install-ai-redis-metrics.sh
bash -n monitoring/v2-staging/install-ai-gpu-metrics.sh
bash -n monitoring/v2-staging/install-gpu-discovery.sh
GPU_PRIVATE_IP=10.30.10.162 GPU_INSTANCE_ID=i-00000000000000000 docker compose -f monitoring/v2-staging/ai-gpu-alloy-compose.yml config --quiet
python3 -m json.tool monitoring/v2-staging/grafana/dashboards/ai-gpu.json >/dev/null
python3 -m unittest discover -s tests/ci -p 'test_gpu_observability.py'
python3 -m json.tool monitoring/v2-staging/alerts.json.tftpl >/dev/null
python3 -m unittest discover -s tests/ci -p 'test_staging_alerts.py'
bash -n monitoring/v2-staging/install-plg-health.sh
python3 -m unittest discover -s tests/ci -p 'test_plg_external_health.py'
python3 -m json.tool monitoring/v2-staging/grafana/dashboards/redis.json.tftpl >/dev/null
python3 -m json.tool monitoring/v2-staging/grafana/dashboards/mysql-validation.json >/dev/null
bash -n monitoring/v2-staging/install-backend-discovery.sh
python3 -m json.tool monitoring/v2-staging/grafana/dashboards/backend.json >/dev/null
python3 -m unittest discover -s tests/ci -p 'test_backend_discovery.py'
python3 -m unittest discover -s tests/ci -p 'test_runpod_update.py'
python3 -m json.tool monitoring/v2-staging/grafana/dashboards/kafka-cdc.json >/dev/null
python3 -m json.tool monitoring/v2-staging/grafana/dashboards/ai-jobs.json >/dev/null
node --check tests/load/scenarios/migration-metrics-smoke.js
python3 -m json.tool monitoring/v2-staging/grafana/dashboards/migration-test.json >/dev/null
node --check tests/load/scenarios/synthetic-auth-smoke.js
bash scripts/validate-image-versions.sh deploy/image-versions.env
python3 scripts/v2-image-manifest.py validate
python3 -m unittest discover -s tests/ci -p 'test_v2_image_manifest.py'
bash -n tests/ci/v2/run-integration.sh
python3 tests/ci/v2/validate.py
python3 -m json.tool monitoring/cloudwatch-agent/app.json >/dev/null
python3 -m json.tool monitoring/v2-staging/grafana/dashboards/v1-source.json >/dev/null
docker compose -f monitoring/v2-staging/plg-compose.yml config --quiet
docker compose -f monitoring/v2-staging/v1-source-alloy-compose.yml config --quiet

temporary_dir="$(mktemp -d)"
trap 'rm -rf -- "${temporary_dir}"' EXIT
python3 tests/ci/render-staging-alert-test.py monitoring/v2-staging/alerts.json.tftpl "${temporary_dir}/alert-tests" tests/ci/staging-alerts.test.json
docker run --rm --network none --entrypoint /bin/promtool -v "${temporary_dir}/alert-tests:/work:ro" -w /work prom/prometheus:v3.14.0@sha256:5ce7540c3c00ef4ab0c9d2c995c6a5b9c421f44b4a115d97a2c7af3b1c21cbb0 test rules tests.json
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

# AI Redis uses a private host address supplied during bootstrap.
AI_REDIS_BIND_IP=10.30.10.1 docker compose -f deploy/v2-staging/ai-redis-compose.yml config --quiet
bash -n terraform/v2/staging/ai-redis-bootstrap.sh.tftpl

# Some Compose versions still require env_file paths with --no-env-resolution.
# Validate the deployment structure using CI-only values, never host secrets.
python3 - "${temporary_env}" "${temporary_dir}/ai-gpu-compose.yml" <<'PYTHON'
import pathlib
import sys

compose = pathlib.Path('deploy/v2-staging/ai-gpu/compose.yml').read_text()
for name in ('embedding', 'runner'):
    compose = compose.replace(f'/etc/yeodam/ai/{name}.env', sys.argv[1])
pathlib.Path(sys.argv[2]).write_text(compose)
PYTHON
AI_GPU_BIND_IP=10.30.10.162 docker compose -f "${temporary_dir}/ai-gpu-compose.yml" config --quiet
bash -n scripts/install-v2-staging-ai-gpu.sh
python3 -c 'import ast,pathlib; ast.parse(pathlib.Path("scripts/configure-v2-staging-ai-gpu.py").read_text())'

python3 -c 'import ast,pathlib; ast.parse(pathlib.Path("scripts/verify-v2-staging-ai-api.py").read_text())'

python3 -m unittest discover -s tests/ci -p 'test_v2_ai_deploy.py'

python3 -m unittest discover -s tests/ci -p 'test_v2_release_event.py'

python3 -m unittest discover -s tests/ci -p 'test_v2_staging_release.py'
