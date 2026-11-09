#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
python3 -m unittest discover -s tests/ci -p 'test_v2_qdrant_migration.py'
image=$(python3 -c 'import sys; sys.path.insert(0,"scripts"); import qdrant_migration as q; print(q.IMAGE)')
helper=$(sed -n "s/^CLIENT_IMAGE='\(.*\)'/\1/p" scripts/restore-v2-qdrant.py)
docker pull "$image"
docker pull "$helper"
python3 tests/ci/smoke-v2-qdrant.py --output "${RUNNER_TEMP:-/tmp}/yeodam-v2-qdrant-results"
