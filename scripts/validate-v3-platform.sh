#!/usr/bin/env bash
# Rendering/schema/contract checks only: no kubeconfig, AWS credentials or apply.
set -Eeuo pipefail
repository_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repository_dir}"
validation_dir="${V3_VALIDATION_DIR:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}/yeodam-v3-validation}"
python3 scripts/prepare-v3-validation.py "${validation_dir}"
if [[ ! -x "${validation_dir}/venv/bin/python" ]]; then
  python3 -m venv "${validation_dir}/venv"
fi
"${validation_dir}/venv/bin/python" -m pip install --disable-pip-version-check \
  -q -r scripts/requirements-v3-validation.txt
export V3_HELM="${validation_dir}/helm"
export V3_KUBECONFORM="${validation_dir}/kubeconform"
export V3_SCHEMA_DIR="${validation_dir}/schemas"
rendered_dir="${V3_RENDERED_DIR:-${validation_dir}/rendered}"
mkdir -p "${rendered_dir}"
"${V3_HELM}" lint --strict deploy/v3/platform
"${validation_dir}/venv/bin/python" scripts/check-v3-platform.py --output "${rendered_dir}/platform.yaml"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_platform.py'
"${V3_HELM}" lint --strict deploy/v3/apps
"${V3_HELM}" lint --strict deploy/v3/apps -f tests/ci/v3/apps-values.fixture.yaml
"${validation_dir}/venv/bin/python" scripts/check-v3-apps.py --output "${rendered_dir}/apps-disabled.yaml"
"${validation_dir}/venv/bin/python" scripts/check-v3-apps.py \
  -f tests/ci/v3/apps-values.fixture.yaml --output "${rendered_dir}/apps-ci-fixture.yaml"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_apps.py'
backend_values=deploy/v3/environments/foundation/backend.values.yaml
kafka_values=deploy/v3/environments/foundation/backend-kafka.values.yaml
backend_fixture=tests/ci/v3/backend-values.fixture.yaml
"${V3_HELM}" lint --strict deploy/v3/apps -f "${backend_values}"
"${validation_dir}/venv/bin/python" scripts/check-v3-apps.py -f "${backend_values}" \
  --output "${rendered_dir}/backend-disabled.yaml"
"${validation_dir}/venv/bin/python" scripts/check-v3-apps.py -f "${backend_values}" -f "${backend_fixture}" \
  --output "${rendered_dir}/backend-ci-fixture.yaml"
"${validation_dir}/venv/bin/python" scripts/check-v3-apps.py -f "${backend_values}" -f "${kafka_values}" -f "${backend_fixture}" \
  --output "${rendered_dir}/backend-kafka-ci-fixture.yaml"
"${validation_dir}/venv/bin/python" scripts/check-v3-apps.py -f "${backend_values}" -f "${backend_fixture}" \
  -f tests/ci/v3/backend-roles-values.fixture.yaml --output "${rendered_dir}/backend-five-roles-ci-fixture.yaml"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_backend.py'
"${V3_HELM}" lint --strict deploy/v3/secret-access
"${V3_HELM}" lint --strict deploy/v3/secret-access -f tests/ci/v3/secrets-values.fixture.yaml
"${validation_dir}/venv/bin/python" scripts/check-v3-secrets.py \
  --directory "${validation_dir}" --output "${rendered_dir}"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_secrets.py'

"${V3_HELM}" lint --strict deploy/v3/storage
"${validation_dir}/venv/bin/python" scripts/check-v3-storage.py --output "${rendered_dir}/storage.yaml"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_storage.py'

"${V3_HELM}" lint --strict deploy/v3/kafka
"${V3_HELM}" lint --strict deploy/v3/kafka -f deploy/v3/environments/foundation/kafka.values.yaml
"${V3_HELM}" lint --strict deploy/v3/kafka -f tests/ci/v3/kafka-smoke.values.yaml
"${validation_dir}/venv/bin/python" scripts/check-v3-kafka.py \
  --directory "${validation_dir}" --output "${rendered_dir}"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_kafka.py'

"${V3_HELM}" lint --strict deploy/v3/mysql
"${V3_HELM}" lint --strict deploy/v3/mysql -f deploy/v3/environments/foundation/mysql.values.yaml
"${validation_dir}/venv/bin/python" scripts/check-v3-mysql.py --output "${rendered_dir}"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_mysql.py'
"${validation_dir}/venv/bin/python" scripts/check-v3-secrets.py \
  --directory "${validation_dir}" --output "${rendered_dir}" \
  -f deploy/v3/environments/foundation/mysql-secrets.values.yaml

"${V3_HELM}" lint --strict deploy/v3/redis
"${V3_HELM}" lint --strict deploy/v3/redis -f deploy/v3/environments/foundation/redis.values.yaml
"${validation_dir}/venv/bin/python" scripts/check-v3-redis.py --output "${rendered_dir}"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_redis.py'
"${validation_dir}/venv/bin/python" scripts/check-v3-secrets.py \
  --directory "${validation_dir}" --output "${rendered_dir}" \
  -f deploy/v3/environments/foundation/mysql-secrets.values.yaml \
  -f deploy/v3/environments/foundation/redis-secrets.values.yaml

"${V3_HELM}" lint --strict deploy/v3/mongodb
"${V3_HELM}" lint --strict deploy/v3/mongodb -f deploy/v3/environments/foundation/mongodb.values.yaml
"${validation_dir}/venv/bin/python" scripts/check-v3-mongodb.py --output "${rendered_dir}"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_mongodb.py'
"${validation_dir}/venv/bin/python" scripts/check-v3-secrets.py \
  --directory "${validation_dir}" --output "${rendered_dir}" \
  -f deploy/v3/environments/foundation/mysql-secrets.values.yaml \
  -f deploy/v3/environments/foundation/redis-secrets.values.yaml \
  -f deploy/v3/environments/foundation/mongodb-secrets.values.yaml

"${V3_HELM}" lint --strict deploy/v3/karpenter-access
"${validation_dir}/venv/bin/python" scripts/check-v3-karpenter.py \
  --directory "${validation_dir}" --output "${rendered_dir}"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_karpenter.py'

"${V3_HELM}" lint --strict deploy/v3/nodepools
"${V3_HELM}" lint --strict deploy/v3/nodepools -f deploy/v3/environments/foundation/nodepools.values.yaml
"${validation_dir}/venv/bin/python" scripts/check-v3-nodepools.py \
  --directory "${validation_dir}" --output "${rendered_dir}"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_nodepools.py'

"${V3_HELM}" lint --strict deploy/v3/kafka-access
"${V3_HELM}" lint --strict deploy/v3/kafka-access -f deploy/v3/environments/foundation/kafka-access.values.yaml
"${validation_dir}/venv/bin/python" scripts/check-v3-kafka-access.py \
  --directory "${validation_dir}" --output "${rendered_dir}"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_kafka_access.py'

"${V3_HELM}" lint --strict deploy/v3/connect
"${validation_dir}/venv/bin/python" scripts/check-v3-connect.py --directory "${validation_dir}" --output "${rendered_dir}"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_connect.py'

"${V3_HELM}" lint --strict deploy/v3/mysql-schema
"${validation_dir}/venv/bin/python" scripts/check-v3-mysql-schema.py --output "${rendered_dir}"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_mysql_schema.py'
"${validation_dir}/venv/bin/python" scripts/check-v3-secrets.py \
  --directory "${validation_dir}" --output "${rendered_dir}" \
  -f deploy/v3/environments/foundation/mysql-cdc-secrets.values.yaml \
  -f deploy/v3/environments/foundation/redis-secrets.values.yaml \
  -f deploy/v3/environments/foundation/mongodb-secrets.values.yaml

"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_validation_download.py'

"${V3_HELM}" lint --strict deploy/v3/outbox
"${validation_dir}/venv/bin/python" scripts/check-v3-outbox.py --output "${rendered_dir}"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_outbox.py'
"${validation_dir}/venv/bin/python" scripts/check-v3-secrets.py \
 --directory "${validation_dir}" --output "${rendered_dir}" \
 -f deploy/v3/environments/foundation/mysql-cdc-secrets.values.yaml \
 -f deploy/v3/environments/foundation/redis-secrets.values.yaml \
 -f deploy/v3/environments/foundation/mongodb-secrets.values.yaml \
 -f deploy/v3/environments/foundation/outbox-secrets.values.yaml

# BE dependency supply and TLS/role contracts; synthetic CA only, no cluster access.
"${V3_HELM}" lint --strict deploy/v3/backend-access
"${validation_dir}/venv/bin/python" scripts/check-v3-backend-access.py
"${validation_dir}/venv/bin/python" scripts/check-v3-apps.py -f deploy/v3/environments/foundation/backend.values.yaml -f deploy/v3/environments/foundation/backend-connections.values.yaml
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_backend_connections.py'
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_backend_rollout.py'
"${validation_dir}/venv/bin/python" scripts/check-v3-secrets.py --directory "${validation_dir}" --output "${rendered_dir}" \
 -f deploy/v3/environments/foundation/mysql-cdc-secrets.values.yaml \
 -f deploy/v3/environments/foundation/redis-secrets.values.yaml \
 -f deploy/v3/environments/foundation/mongodb-secrets.values.yaml \
 -f deploy/v3/environments/foundation/outbox-secrets.values.yaml \
 -f deploy/v3/environments/foundation/backend-secrets.values.yaml

# Standalone WebSocket release: no BE deployment or secret source changes.
"${V3_HELM}" lint --strict deploy/v3/apps -f deploy/v3/environments/foundation/websocket.values.yaml
"${V3_HELM}" lint --strict deploy/v3/websocket-access
"${validation_dir}/venv/bin/python" scripts/check-v3-apps.py -f deploy/v3/environments/foundation/websocket.values.yaml
"${validation_dir}/venv/bin/python" scripts/check-v3-websocket.py
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_websocket.py'

# FE and validation ALB only; no AWS calls or controller deployment.
"${V3_HELM}" lint --strict deploy/v3/apps -f deploy/v3/environments/foundation/frontend.values.yaml
"${V3_HELM}" lint --strict deploy/v3/external-access
"${validation_dir}/venv/bin/python" scripts/check-v3-apps.py -f deploy/v3/environments/foundation/frontend.values.yaml
"${validation_dir}/venv/bin/python" scripts/check-v3-external.py --controller-directory "${validation_dir}" --output "${rendered_dir}/load-balancer-controller-public.yaml"
"${validation_dir}/venv/bin/python" scripts/check-v3-external.py
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_external.py'

# Isolated AI stores: no production values or cluster access.
"${V3_HELM}" lint --strict deploy/v3/ai-storage
"${validation_dir}/venv/bin/python" scripts/check-v3-ai-storage.py --output "${rendered_dir}"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_ai_storage.py'
"${validation_dir}/venv/bin/python" scripts/check-v3-secrets.py --directory "${validation_dir}" --output "${rendered_dir}" -f deploy/v3/environments/foundation/ai-storage-secrets.values.yaml

# AI source/runtime preparation: no models or live services.
"${V3_HELM}" lint --strict deploy/v3/ai-apps
"${validation_dir}/venv/bin/python" scripts/check-v3-ai.py --output "${rendered_dir}"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_ai.py'
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_runpod.py'
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_legacy_ai_rollback.py'
"${validation_dir}/venv/bin/python" scripts/check-v3-secrets.py --directory "${validation_dir}" --output "${rendered_dir}" -f deploy/v3/environments/foundation/ai-secrets.values.yaml

"${validation_dir}/venv/bin/python" scripts/check-v3-argocd.py --directory "${validation_dir}" --output "${rendered_dir}"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p "test_v3_argocd.py"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p "test_v3_gitops_release.py"

# Frozen, disabled BE/AI preparation inputs; no source checkout or credentials in CI.
"${validation_dir}/venv/bin/python" scripts/check-v3-app-candidate.py > "${rendered_dir}/be-ai-candidate-plan.json"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p test_v3_app_candidate.py
"${V3_HELM}" template yeodam-v3-be-candidate deploy/v3/apps --kube-version 1.36.0 -f deploy/v3/environments/foundation/backend.values.yaml -f deploy/v3/environments/foundation/backend-connections.values.yaml -f deploy/v3/candidates/be-ai/backend.values.yaml > "${rendered_dir}/be-candidate-disabled.yaml"
"${V3_HELM}" template yeodam-v3-ai-candidate deploy/v3/ai-apps --kube-version 1.36.0 -f deploy/v3/candidates/be-ai/ai.values.yaml > "${rendered_dir}/ai-candidate-disabled.yaml"
"${validation_dir}/venv/bin/python" -c 'import sys,yaml; sys.exit(any(d is not None for p in sys.argv[1:] for d in yaml.safe_load_all(open(p))))' "${rendered_dir}/be-candidate-disabled.yaml" "${rendered_dir}/ai-candidate-disabled.yaml"

"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p test_v3_model_path.py

# Public Service identity review and exact egress aliases. No live discovery.
"${V3_HELM}" lint --strict deploy/v3/service-access
"${validation_dir}/venv/bin/python" scripts/check-v3-service-access.py --output "${rendered_dir}"
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p test_v3_service_access.py

# Complete approved app snapshots and observer boundaries; fixture data only.
"${validation_dir}/venv/bin/python" scripts/prepare-v3-release-candidate.py
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p test_v3_delivery.py

# Actual CDC task and full-route evidence must gate QA independently of app health.
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p test_v3_cdc_recovery.py
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p test_v3_qa_readiness.py

"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v2_v3_cutover_render.py'

python3 -m unittest discover -s tests/ci -p "test_v3_schema_upgrade.py"

# AI stdout scope, masking and dedicated authority; no application changes.
"${V3_HELM}" lint --strict deploy/v3/ai-logs
"${validation_dir}/venv/bin/python" scripts/check-v3-ai-logs.py
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_ai_logs.py'

# Minimal metrics on existing nodes; no secret read or live changes.
"${V3_HELM}" lint --strict deploy/v3/metrics
"${validation_dir}/venv/bin/python" scripts/check-v3-metrics.py
"${validation_dir}/venv/bin/python" -m unittest discover -s tests/ci -p 'test_v3_metrics.py'
