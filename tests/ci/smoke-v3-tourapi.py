"""Run inside a candidate image with no network; validate the opt-in workflow implementation."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

if os.environ.get('TOURAPI_REVIEW_REQUIRED') != 'true':
    print('TourAPI disabled in candidate; previous image preserved')
    raise SystemExit(0)

from app.jobs.config import JobsSettings
from app.pipeline.workflow import resources
from app.infra.place_index import ensure_local_index

contract = json.loads(Path('/contract.json').read_text())
settings = dict(contract['commonEnvironment'], **contract['tourapiEnvironment'],
                **contract['workloads']['runner']['environment'], **contract['tourapiRunnerEnvironment'])
# Settings only: no clients, models, queues or credentials are accessed.
cfg = JobsSettings(_env_file=None, API_KEY='synthetic-only', KAFKA_SASL_PASSWORD='synthetic-only', QDRANT_API_KEY='synthetic-only', **settings)
assert cfg.PROCESS_WORKFLOW_ENABLED
assert cfg.PROCESS_WORKFLOW_BUDGET_SEC < cfg.JOBS_PROCESS_TIMEOUT_SEC
assert cfg.PROCESS_WORKFLOW_CONCURRENCY == 4
assert cfg.GPU_PROCESS_GENERATION_INFLIGHT == cfg.GPU_STORY_GENERATION_INFLIGHT == 4
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / 'tour.json'
    payload = b'{"records":[{"contentid":"synthetic"}]}'
    digest = hashlib.sha256(payload).hexdigest()
    def download(target): Path(target).write_bytes(payload)
    # Exercise the real atomic checksum helper used by TourAPI without S3.
    ensure_local_index(path, digest, download)
    synthetic = SimpleNamespace(PROCESS_WORKFLOW_ENABLED=True, TOURAPI_RAW_PATH=str(path),
        TOURAPI_RAW_S3_URI=None, TOURAPI_RAW_SHA256=digest, AWS_REGION='ap-northeast-2')
    assert resources.prepare_tour_raw(synthetic) == path
    assert resources.load_tour_raw(str(path))[0]['contentid'] == 'synthetic'
    path.write_text('invalid JSON')
    try: resources.prepare_tour_raw(synthetic)
    except (ValueError, StopIteration): pass
    else: raise AssertionError('Corrupt JSON accepted')
    path.unlink()
    try: ensure_local_index(path, '0' * 64, download)
    except (ValueError, RuntimeError): pass
    else: raise AssertionError('Wrong checksum accepted')
print('Exact candidate: workflow settings, checksum and JSON preparation passed without network')
