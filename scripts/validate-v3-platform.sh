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
