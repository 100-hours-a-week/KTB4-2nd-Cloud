"""Cloud process adapter: stop new claims, finish current work, resume on lease expiry.

No AI repository changes. Wrap the existing runner_loop with its supported stop Event;
never cancel active work to perform a URL update. State is local, temporary and public.
"""
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

DIRECTORY = Path('/tmp/yeodam-runpod-control')
LEASE = DIRECTORY / 'lease.json'
STATE = DIRECTORY / 'state.json'
SOURCE = Path('/var/run/yeodam-vlm/base-url')
PROTOCOL = 1


def atomic(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix('.' + str(os.getpid()) + '.tmp')
    temporary.write_text(json.dumps(data))
    temporary.chmod(0o600)
    temporary.replace(path)


def lease():
    try:
        value = json.loads(LEASE.read_text())
        if re.fullmatch('[a-f0-9]{32}', value['operation']) and time.time() < value['expires'] <= time.time() + 3601:
            return value
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


class Control:
    def __init__(self, expected):
        self.expected = expected
        self.loops = {}
        self.http = 0
        self.monitor_task = None

    async def monitor(self):
        while True:
            current = lease()
            if current:
                for stop, _ in self.loops.values():
                    stop.set()
            atomic(STATE, {'protocol': PROTOCOL, 'operation': current['operation'] if current else None,
                           'draining': bool(current), 'idle': bool(current) and len(self.loops) == self.expected
                           and all(done for _, done in self.loops.values()) and self.http == 0,
                           'loops': len(self.loops), 'activeHttp': self.http})
            await asyncio.sleep(.2)

    def start(self):
        if self.monitor_task is None:
            self.monitor_task = asyncio.create_task(self.monitor())

    def wrap(self, original):
        async def controlled(store, kind, stop=None):
            self.start()
            key = object()
            try:
                while stop is None or not stop.is_set():
                    local = asyncio.Event()
                    self.loops[key] = (local, bool(lease()))
                    while lease():
                        if stop is not None and stop.is_set():
                            return
                        await asyncio.sleep(.2)
                    local = asyncio.Event()
                    self.loops[key] = (local, False)
                    async def stop_parent():
                        while not local.is_set():
                            if lease() or (stop is not None and stop.is_set()):
                                local.set()
                            await asyncio.sleep(.1)
                    watch = asyncio.create_task(stop_parent())
                    try:
                        await original(store, kind, local)
                    finally:
                        watch.cancel()
                        await asyncio.gather(watch, return_exceptions=True)
                    self.loops[key] = (local, True)
                    if stop is not None and stop.is_set():
                        return
                    # Keep the supervisor alive while drained; Kubernetes must not
                    # restart it automatically before the update controller decides.
                    if not local.is_set():
                        raise RuntimeError('AI loop returned without a drain request')
            finally:
                self.loops.pop(key, None)
        return controlled

    def middleware(self, application):
        async def controlled(scope, receive, send):
            if scope['type'] != 'http' or scope.get('path') in {'/health', '/metrics'}:
                return await application(scope, receive, send)
            if lease():
                await send({'type': 'http.response.start', 'status': 503,
                            'headers': [(b'content-type', b'application/json'), (b'retry-after', b'30')]})
                return await send({'type': 'http.response.body', 'body': b'{"error":"model_maintenance"}'})
            self.http += 1
            try:
                await application(scope, receive, send)
            finally:
                self.http -= 1
        return controlled


def install(expected):
    from app.jobs import runner
    control = Control(expected)
    runner.runner_loop = control.wrap(runner.runner_loop)
    return control


def cli():
    action = sys.argv[1]
    if action == 'drain':
        operation, ttl = sys.argv[2], int(sys.argv[3])
        assert re.fullmatch('[a-f0-9]{32}', operation) and 30 <= ttl <= 3600
        current = lease()
        assert current is None or current['operation'] == operation
        atomic(LEASE, {'operation': operation, 'expires': time.time() + ttl})
        result = {'requested': True}
    elif action == 'resume':
        current = lease()
        if current:
            assert current['operation'] == sys.argv[2]
            LEASE.unlink()
        result = {'resumed': True}
    elif action == 'status':
        result = json.loads(STATE.read_text())
        current = lease()
        if not current or current['operation'] != result.get('operation'):
            result.update(idle=False, draining=False)
        else:
            result['leaseRemainingSeconds'] = int(current['expires'] - time.time())
    elif action == 'source':
        result = {'sourceMatches': hashlib.sha256(SOURCE.read_bytes().strip()).hexdigest() == sys.argv[2]}
    elif action == 'environment':
        result = {'environmentMatches': hashlib.sha256(os.environ['VLM_BASE_URL'].encode()).hexdigest() == sys.argv[2], 'modelMatches': os.environ['VLM_MODEL'] == 'gemma-4'}
    elif action == 'verify':
        import urllib.request
        url = os.environ['VLM_BASE_URL']
        assert hashlib.sha256(url.encode()).hexdigest() == sys.argv[2]
        assert re.fullmatch(r'https://[a-z0-9]+-8000\.proxy\.runpod\.net/v1', url)
        # Match the deployed AI client identification; the default urllib agent is rejected upstream.
        request = urllib.request.Request(url + '/models', headers={'Authorization': 'Bearer ' + os.environ['VLM_API_KEY'], 'User-Agent': 'python-httpx/0.28.1'})
        # No redirects: never send the authorization to a different endpoint.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
            data = json.loads(response.read(1024 * 1024))
        result = {'environmentMatches': True,
                  'modelReady': any(x.get('id') == os.environ['VLM_MODEL'] for x in data.get('data', []))}
    else:
        raise ValueError('Unknown bounded control action')
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        cli()
    except Exception:
        raise SystemExit('AI control failed; sensitive output suppressed') from None
