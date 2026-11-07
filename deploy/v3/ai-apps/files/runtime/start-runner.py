"""Prepare and report the existing index before exec; never load a GPU model here."""
import os,sys
from pathlib import Path
from app.jobs.config import JobsSettings
from app.infra.place_index import prepare_place_index
cfg=JobsSettings()
path=prepare_place_index(cfg)
if path is None or path.stat().st_size!=1098801152:raise SystemExit('Reference index not ready')
Path('/tmp/place-index-ready').write_text(cfg.PLACE_INDEX_SHA256)
print('Place index ready: bytes=1098801152',flush=True)
os.execv(sys.executable,[sys.executable,'-m','app.jobs.runner'])
