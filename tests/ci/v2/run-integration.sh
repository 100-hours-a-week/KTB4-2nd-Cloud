#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

python3 scripts/v2-image-manifest.py validate
runtime_dir=$(mktemp -d)
export V2_CI_RUNTIME="$runtime_dir"
export V2_CI_RESULTS_DIRECTORY="${V2_CI_RESULTS_DIRECTORY:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}/yeodam-v2-ci-results-$$}"
mkdir -p "$V2_CI_RESULTS_DIRECTORY" "$runtime_dir/cdc-libs"
# The image's unprivileged AI user writes synthetic reports only.
chmod 0777 "$V2_CI_RESULTS_DIRECTORY"
project="yeodam-v2-ci-${GITHUB_RUN_ID:-local}-$$"
compose=(docker compose --project-name "$project" --env-file "$runtime_dir/images.env" -f tests/ci/v2/compose.yml)

cleanup() {
  result=$?
  trap - EXIT
  if [[ -f "$runtime_dir/images.env" ]]; then
    "${compose[@]}" ps --all > "$V2_CI_RESULTS_DIRECTORY/containers.txt" 2>&1 || true
    "${compose[@]}" logs --no-color --tail=120 > "$V2_CI_RESULTS_DIRECTORY/services.log" 2>&1 || true
    "${compose[@]}" down --volumes --remove-orphans --timeout 15 >/dev/null 2>&1 || true
  fi
  rm -rf -- "$runtime_dir"
  echo "V2 CI results: $V2_CI_RESULTS_DIRECTORY"
  exit "$result"
}
trap cleanup EXIT

python3 - "$runtime_dir" "$V2_CI_RESULTS_DIRECTORY" <<'PY'
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

runtime, results = map(Path, sys.argv[1:])
manifest = json.loads(Path('deploy/v2/image-manifest.json').read_text())
keys = {'frontend': 'FRONTEND', 'backend': 'BACKEND', 'ai-app': 'AI_APP'}
lines = []
for name, entry in manifest['components'].items():
    package = entry['image'].split(':sha-', 1)[0]
    lines.append(f'V2_{keys[name]}_IMAGE={package}@{entry["digest"]}')
(runtime / 'images.env').write_text('\n'.join(lines) + '\n')
(results / 'manifest.json').write_text(json.dumps(manifest, indent=2))
# Exactly the Groovy runtime used by the deployed Connect image's CDC Filter.
artifacts = {
    'groovy': 'd77815c8f31d503bc50129f28892e60b64e339b490f7babf9df70848ce465698',
    'groovy-jsr223': '73b24efc770f471d11fb33afde0105765467b21f62db89336259b593c79de745',
}
for artifact, checksum in artifacts.items():
    name = artifact + '-4.0.32.jar'
    url = f'https://repo.maven.apache.org/maven2/org/apache/groovy/{artifact}/4.0.32/{name}'
    data = urllib.request.urlopen(url, timeout=30).read()
    assert hashlib.sha256(data).hexdigest() == checksum, 'Groovy checksum mismatch'
    (runtime / 'cdc-libs' / name).write_bytes(data)
PY

python3 scripts/v2-image-manifest.py verify
"${compose[@]}" config --quiet
"${compose[@]}" up -d --wait --wait-timeout 300 mysql redis-auth redis-ai s3 kafka qdrant models
"${compose[@]}" exec -T s3 awslocal s3 mb s3://yeodam-v2-ci >/dev/null
"${compose[@]}" run --rm -T driver /ci/prepare.py topics
"${compose[@]}" up -d --wait --wait-timeout 300 ai-api process-runner backend frontend
"${compose[@]}" exec -T mysql mysql -uroot -pci-root-only yeodam_v2_ci < tests/ci/v2/seed.sql
"${compose[@]}" up -d --wait --wait-timeout 180 connect
"${compose[@]}" run --rm -T driver /ci/prepare.py connector
"${compose[@]}" run --rm -T driver /ci/smoke.py

trip=$(python3 -c 'import json,os; from pathlib import Path; n=json.loads((Path(os.environ["V2_CI_RESULTS_DIRECTORY"])/"journey.json").read_text())["tripId"]; assert type(n) is int and n>0; print(n)')
"${compose[@]}" exec -T mysql mysql -uroot -pci-root-only yeodam_v2_ci -N -B -e "
SELECT JSON_OBJECT(
 'tripId', trip_id, 'status', processing_status,
 'executionStatus', (SELECT status FROM trip_creation_executions WHERE trip_id=$trip),
 'savedPhotos', (SELECT COUNT(*) FROM trip_attachments WHERE trip_id=$trip),
 'outboxCount', (SELECT COUNT(*) FROM trip_creation_outbox WHERE trip_id=$trip),
 'published', (SELECT COUNT(*) FROM trip_creation_outbox WHERE trip_id=$trip AND status='PUBLISHED'),
 'cdcManaged', (SELECT COUNT(*) FROM trip_creation_outbox WHERE trip_id=$trip AND cdc_managed=1)
) FROM trips WHERE trip_id=$trip;" > "$V2_CI_RESULTS_DIRECTORY/database.json"
python3 - <<'PY'
import json
import os
from pathlib import Path
directory = Path(os.environ['V2_CI_RESULTS_DIRECTORY'])
data = json.loads((directory / 'database.json').read_text())
assert data['status'] == data['executionStatus'] == 'COMPLETED', data
assert data['savedPhotos'] == 2, data
assert data['outboxCount'] >= 2 and data['outboxCount'] == data['published'] == data['cdcManaged'], data
print('V2_CI_PASSED: presigned upload, Outbox CDC, AI handler, Kafka result and BE persistence')
PY
