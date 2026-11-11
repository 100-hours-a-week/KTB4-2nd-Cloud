"""Prepare the existing index, then run the controlled process loop; never load a GPU model here."""
import os,sys,asyncio,signal
from pathlib import Path
from app.jobs.config import JobsSettings
from app.infra.place_index import prepare_place_index
from reference_data import prepare_tourapi
Path('/tmp/place-index-ready').unlink(missing_ok=True)
Path('/tmp/tourapi-ready').unlink(missing_ok=True)
cfg=JobsSettings()
path=prepare_place_index(cfg)
if path is None or path.stat().st_size!=1098801152:raise SystemExit('Reference index not ready')
prepare_tourapi(cfg)
Path('/tmp/place-index-ready').write_text(cfg.PLACE_INDEX_SHA256)
print('Place index ready: bytes=1098801152',flush=True)
from runpod_control import install
install(1)
from app.jobs import runner
async def serve():
    task=asyncio.create_task(runner.main())
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,task.cancel)
    try:await task
    except asyncio.CancelledError:pass
asyncio.run(serve())
