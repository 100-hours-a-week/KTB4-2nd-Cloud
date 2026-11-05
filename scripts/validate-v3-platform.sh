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
