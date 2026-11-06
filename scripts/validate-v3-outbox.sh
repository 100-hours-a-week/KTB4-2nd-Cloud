#!/usr/bin/env bash
# Synthetic-only runtime; no AWS, kubeconfig, registry login or real data.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
context="${V3_OUTBOX_CONTEXT:-${RUNNER_TEMP:-/tmp}/yeodam-v3-outbox-build}"
result="${V3_OUTBOX_RESULTS:-${RUNNER_TEMP:-/tmp}/yeodam-v3-outbox-results}"
image="${V3_OUTBOX_IMAGE:-yeodam-v3-connect:outbox-ci}"
python3 scripts/prepare-v3-connect-image.py --directory "$context"
docker build --platform linux/amd64 --provenance=false \
 --label org.opencontainers.image.source=https://github.com/100-hours-a-week/KTB4-2nd-Cloud \
 --label "org.opencontainers.image.revision=${GITHUB_SHA:-local-validation}" -t "$image" "$context"
python3 tests/ci/smoke-v3-outbox.py --image "$image" --output "$result"
