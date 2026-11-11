"""Prepare TourAPI before Ready/claiming; old images remain usable with opt-in disabled."""
import os
from pathlib import Path

SIZE = 34351861
MARKER = Path('/tmp/tourapi-ready')

def prepare_tourapi(cfg):
    MARKER.unlink(missing_ok=True)
    if os.environ.get('CLOUD_TOURAPI_ENABLED') != '1':
        return
    if not getattr(cfg, 'PROCESS_WORKFLOW_ENABLED', False):
        raise RuntimeError('TourAPI requires workflow support in the selected image')
    from app.pipeline.workflow.resources import prepare_tour_raw
    path = prepare_tour_raw(cfg)  # Existing atomic download, checksum and JSON validation.
    if path is None or path != Path(os.environ['TOURAPI_RAW_PATH']) or path.stat().st_size != SIZE:
        raise RuntimeError('TourAPI reference not ready')
    MARKER.write_text(os.environ['TOURAPI_RAW_SHA256'])
    print(f'TourAPI ready: bytes={SIZE}', flush=True)

def tourapi_ready():
    if os.environ.get('CLOUD_TOURAPI_ENABLED') != '1':
        return True
    path = Path(os.environ['TOURAPI_RAW_PATH'])
    return (path.is_file() and path.stat().st_size == SIZE and MARKER.is_file()
            and MARKER.read_text() == os.environ['TOURAPI_RAW_SHA256'])
