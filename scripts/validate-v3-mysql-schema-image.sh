#!/usr/bin/env bash
# Synthetic Docker-only checks; no AWS, registry login or Kubernetes access.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
context="${V3_SCHEMA_CONTEXT:-${RUNNER_TEMP:-/tmp}/yeodam-v3-mysql-schema-build}"
result="${V3_SCHEMA_RESULTS:-${RUNNER_TEMP:-/tmp}/yeodam-v3-mysql-schema-results}"
image="${V3_SCHEMA_IMAGE:-yeodam-v3-mysql-schema:ci}"
python3 scripts/prepare-v3-mysql-schema-image.py --directory "$context"
docker build --platform linux/amd64 --provenance=false \
  --label "org.opencontainers.image.revision=${GITHUB_SHA:-local-validation}" -t "$image" "$context"
python3 tests/ci/smoke-v3-mysql-schema.py --image "$image" --output "$result"
