#!/usr/bin/env bash
# Docker/synthetic data only. No registry login, AWS, kubeconfig or image push here.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
context="${V3_CONNECT_CONTEXT:-${RUNNER_TEMP:-/tmp}/yeodam-v3-connect-build}"
result="${V3_CONNECT_RESULTS:-${RUNNER_TEMP:-/tmp}/yeodam-v3-connect-results}"
image="${V3_CONNECT_IMAGE:-yeodam-v3-connect:ci}"
python3 scripts/prepare-v3-connect-image.py --directory "$context"
docker build --platform linux/amd64 --provenance=false \
  --label org.opencontainers.image.source=https://github.com/100-hours-a-week/KTB4-2nd-Cloud \
  --label "org.opencontainers.image.revision=${GITHUB_SHA:-local-validation}" -t "$image" "$context"
python3 tests/ci/smoke-v3-connect.py --image "$image" --output "$result"
