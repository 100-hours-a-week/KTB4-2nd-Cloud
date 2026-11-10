"""Run inside the exact candidate AI image, no network/Redis/models/user records."""
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace

import runpod_control as control_module
from app.jobs import runner


async def scenario(kind, photo):
    with tempfile.TemporaryDirectory() as directory:
        control_module.DIRECTORY = Path(directory)
        control_module.LEASE = Path(directory) / 'lease.json'
        control_module.STATE = Path(directory) / 'state.json'
        control = control_module.Control(1)
        started = asyncio.Event(); finish = asyncio.Event()
        calls = []; failures = []
        cfg = SimpleNamespace(JOBS_PROCESS_STANDBY=0, JOBS_POLL_SEC=.01, EMBED_ON_PHOTOS_READY=photo)
        class Store:
            def __init__(self): self.cfg = cfg
            async def recover(self): pass
            async def claim(self, *args, **kwargs):
                if photo: return None
                return SimpleNamespace(identity='fixture', owner='fixture')
            async def claim_batch(self): return SimpleNamespace(identity='fixture', batch_no=0)
            async def finish(self, *args, **kwargs): failures.append(True)
        async def work(*args):
            calls.append(True); started.set(); await finish.wait()
        originals = (runner.handler_for, runner.run_claimed, runner.run_photo_batch)
        runner.handler_for = lambda *_: None
        runner.run_claimed = work
        runner.run_photo_batch = work
        task = asyncio.create_task(control.wrap(runner.runner_loop)(Store(), kind))
        try:
            await asyncio.wait_for(started.wait(), 3)
            control_module.atomic(control_module.LEASE, {'operation': 'a' * 32, 'expires': time.time() + 60})
            await asyncio.sleep(.3)
            assert not json.loads(control_module.STATE.read_text())['idle']
            finish.set()
            for _ in range(100):
                if json.loads(control_module.STATE.read_text())['idle']: break
                await asyncio.sleep(.02)
            assert json.loads(control_module.STATE.read_text())['idle']
            assert len(calls) == 1 and not failures and not task.done()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if control.monitor_task:
                control.monitor_task.cancel()
                await asyncio.gather(control.monitor_task, return_exceptions=True)
            runner.handler_for, runner.run_claimed, runner.run_photo_batch = originals


async def main():
    for kind, photo in [('process', False), ('process', True), ('story', False)]:
        await scenario(kind, photo)
    # Import the real ASGI factory without running its network lifespan.
    from app.v2_main import create_app
    assert callable(create_app)
    print('Exact AI image: process, photo batch, story drain and API factory compatible; no external calls')


asyncio.run(main())
