"""Run-scoped HTTP proxy with a bounded, pre-dispatch cutover barrier."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import socket as sockets
import tempfile
import time
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, web
from yarl import URL
from multidict import CIMultiDict

from .config import load

HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade', 'proxy-connection'}


def headers(raw):
    blocked = HOP | {v.strip().lower() for v in raw.get('Connection', '').split(',')}
    return CIMultiDict((k, v) for k, v in raw.items() if k.lower() not in blocked)


class Gate:
    def __init__(self, config):
        self.config = config
        self.changed = asyncio.Condition()
        self.phase = 'V1'
        self.inflight = {'V1': 0, 'V2': 0}
        self.waiters = self.wait_bytes = 0
        self.deadline = 0.0
        self.token = None
        self.aborts = 0
        self.reason = None
        self.sequence = 0
        self.terminal = {}
        self.watchdog = None
        self.dispatched = {"V1": 0, "V2": 0}
        self.samples = {"wait": [], "duration": []}
        self.identity = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        self.restore()

    def restore(self):
        path = Path(self.config['journal'])
        if not path.exists():
            return
        if path.stat().st_mode & 0o077:
            raise ValueError('journal must be private')
        previous = json.loads(path.read_text())
        if previous['run_id'] != self.config['run_id'] or previous['config_hash'] != self.identity:
            raise ValueError('journal belongs to a different run/configuration')
        if self.config.get('environment') == 'staging-preparation' and previous['phase'] != 'V1':
            raise ValueError('preparation profile cannot resume committed V2 authority')
        self.phase = previous['phase']  # COMMIT is durable before any V2 dispatch.
        self.reason = 'recovered_committed' if self.phase == 'V2' else 'recovered_precommit'

    def persist(self, phase):
        dest = Path(self.config['journal'])
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if dest.parent.stat().st_mode & 0o077:
            raise ValueError('journal directory must be private')
        fd, name = tempfile.mkstemp(dir=dest.parent, prefix='.journal-')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump({'run_id': self.config['run_id'], 'config_hash': self.identity,
                           'phase': phase, 'committed_at': time.time()}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, dest)
            directory = os.open(dest.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def event(self, event, **fields):
        self.sequence += 1
        print(json.dumps({'source': 'yeodam-request-cutover', 'run_id': self.config['run_id'],
                          'sequence': self.sequence, 'time': time.time(), 'event': event,
                          **fields}, separators=(',', ':')), flush=True)

    def status(self):
        return {'run_id': self.config['run_id'], 'phase': self.phase,
                'hold_token': self.token, 'inflight': dict(self.inflight),
                'waiters': self.waiters, 'wait_bytes': self.wait_bytes,
                'remaining_seconds': max(0, self.deadline - time.monotonic()) if self.phase == 'HOLD' else 0,
                'aborts': self.aborts, 'last_reason': self.reason, 'outcomes': dict(self.terminal)}

    def abort_locked(self, reason):
        if self.phase not in ('V1', 'HOLD'):
            raise ValueError('V2 or uncertain authority is committed; automatic V1 rollback is prohibited')
        self.phase = 'V1'
        self.reason = reason
        self.token = None
        self.aborts += 1
        self.event('aborted', reason=reason)
        self.changed.notify_all()

    async def begin(self):
        if self.config.get('environment') == 'staging-preparation':
            raise ValueError('preparation profile cannot HOLD or import authentication')
        async with self.changed:
            if self.phase != 'V1':
                raise ValueError('begin requires V1 state')
            self.phase = 'HOLD'
            self.token = str(uuid4())
            self.reason = None
            self.deadline = time.monotonic() + self.config.get('hold_seconds', 5)
            self.event('holding')
            self.watchdog = asyncio.create_task(self.expire(self.token))
            return self.status()

    async def expire(self, token):
        await asyncio.sleep(max(0, self.deadline - time.monotonic()))
        async with self.changed:
            if self.phase == 'HOLD' and self.token == token:
                self.abort_locked('hold_timeout')

    async def commit(self, proof):
        async with self.changed:
            if (self.phase != 'HOLD' or self.inflight['V1'] != 0
                    or time.monotonic() >= self.deadline
                    or proof.get('hold_token') != self.token
                    or proof.get('run_id') != self.config['run_id']
                    or type(proof.get('keys')) is not int or proof['keys'] < 0
                    or proof.get('source_fingerprint') != proof.get('target_fingerprint')
                    or not re.fullmatch('[0-9a-f]{64}', proof.get('target_fingerprint', ''))
                    or proof.get('pending_jobs') != 0):
                raise ValueError('unverified, expired or superseded cutover proof')
            try:
                self.persist('V2')
            except OSError:
                # A rename may already have happened. Never resume old auth writes
                # after an uncertain durable commit; operator recovery is required.
                self.phase = 'BLOCKED'
                self.reason = 'commit_journal_uncertain'
                self.token = None
                self.changed.notify_all()
                raise
            self.phase = 'V2'
            self.reason = None
            self.event('committed', keys=proof['keys'])
            self.changed.notify_all()
            return self.status()

    async def admit(self, request):
        started = time.monotonic()
        size = request.content_length
        if size is None and not request.can_read_body:
            size = 0
        reserved = False
        async with self.changed:
            if self.phase == 'HOLD':
                if request.content_type == 'multipart/form-data':
                    self.abort_locked('legacy_multipart_arrived')
                elif size is None:
                    self.abort_locked('unknown_body_size')
                elif (self.waiters >= self.config.get('max_waiters', 1000)
                      or self.wait_bytes + size > self.config.get('max_wait_bytes', 67108864)):
                    self.abort_locked('hold_capacity')
                else:
                    self.waiters += 1
                    self.wait_bytes += size
                    reserved = True
            try:
                while self.phase == 'HOLD':
                    if request.transport is None or request.transport.is_closing():
                        self.abort_locked('waiting_client_disconnected')
                        raise ConnectionResetError()
                    try:
                        await asyncio.wait_for(self.changed.wait(), timeout=0.05)
                    except TimeoutError:
                        pass
                destination = self.phase
                if destination not in self.inflight:
                    raise ValueError('commit journal requires operator recovery')
                self.inflight[destination] += 1
                return destination, time.monotonic() - started
            finally:
                if reserved:
                    self.waiters -= 1
                    self.wait_bytes -= size

    async def proxy(self, request):
        reqid = request.headers.get('X-Request-ID', '')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', reqid):
            reqid = str(uuid4())
        started = time.monotonic()
        dest = None
        waited = 0.0
        status = 0
        outcome = 'client_canceled'
        response = None
        self.event('accepted', request_id=reqid, method=request.method)
        try:
            dest, waited = await self.admit(request)
            base = (self.config['v2_origin'] if dest == 'V2' else
                    self.config['v1_api'] if request.path == '/api' or request.path.startswith('/api/')
                    else self.config['v1_web'])
            forwarded = headers(request.headers)
            forwarded['Host'] = self.config['public_host']
            forwarded['X-Request-ID'] = reqid
            forwarded['X-Forwarded-Proto'] = 'https'
            forwarded['X-Forwarded-Host'] = self.config['public_host']

            async def body():
                async for data in request.content.iter_chunked(65536):
                    yield data

            self.dispatched[dest] += 1
            self.observe('wait', waited)
            self.event('forwarded', request_id=reqid, destination=dest, wait_seconds=waited)
            async with request.app['session'].request(
                    request.method, URL(base + request.rel_url.raw_path_qs, encoded=True),
                    headers=forwarded, data=body() if request.can_read_body else None,
                    allow_redirects=False,
                    server_hostname=self.config.get('v2_tls_name') if dest == 'V2' else None) as upstream:
                status = upstream.status
                outbound = headers(upstream.headers)
                outbound['X-Request-ID'] = reqid
                outbound['X-Yeodam-Cutover-Target'] = dest
                response = web.StreamResponse(status=upstream.status, headers=outbound)
                await response.prepare(request)
                async for data in upstream.content.iter_chunked(65536):
                    await response.write(data)
                await response.write_eof()
                outcome = 'completed'
                return response
        except (asyncio.CancelledError, ConnectionResetError):
            raise
        except Exception:
            outcome = 'upstream_unknown' if dest else 'not_forwarded_error'
            if response is not None and response.prepared:
                response.force_close()
                return response
            return web.json_response({'message': 'CUTOVER_UPSTREAM_UNAVAILABLE'}, status=502)
        finally:
            async with self.changed:
                if dest:
                    self.inflight[dest] -= 1
                self.terminal[outcome] = self.terminal.get(outcome, 0) + 1
                if self.phase == 'HOLD' and outcome != 'completed':
                    self.abort_locked('inflight_request_not_completed')
                self.changed.notify_all()
            elapsed = time.monotonic() - started
            self.observe('duration', elapsed)
            self.event(outcome, request_id=reqid, destination=dest, status=status,
                       wait_seconds=waited, duration_seconds=elapsed)

    def observe(self, name, value):
        # Fixed cumulative histogram, no per-request labels or unbounded sample retention.
        buckets = self.samples[name]
        if not buckets:
            buckets.extend([0] * 12)
        for index, bound in enumerate((.005, .01, .025, .05, .1, .25, .5, 1, 2, 5)):
            if value <= bound:
                buckets[index] += 1
        buckets[10] += 1
        buckets[11] += value

    def metrics(self, values):
        lines = [f'yeodam_request_cutover_{k} {v}' for k, v in values.items()]
        for target, count in self.dispatched.items():
            lines.append(f'yeodam_request_cutover_forwarded_total{{target="{target}"}} {count}')
        for outcome, count in self.terminal.items():
            lines.append(f'yeodam_request_cutover_outcomes_total{{outcome="{outcome}"}} {count}')
        for name, counts in self.samples.items():
            counts = counts or [0] * 12
            metric = f'yeodam_request_cutover_{name}_seconds'
            lines.append(f'# TYPE {metric} histogram')
            for index, bound in enumerate((.005, .01, .025, .05, .1, .25, .5, 1, 2, 5)):
                lines.append(f'{metric}_bucket{{le="{bound}"}} {counts[index]}')
            lines.extend([f'{metric}_bucket{{le="+Inf"}} {counts[10]}',
                          f'{metric}_count {counts[10]}', f'{metric}_sum {counts[11]}'])
        return '\n'.join(lines) + '\n'

    async def control(self, request):
        try:
            if request.path == '/status':
                return web.json_response(self.status())
            if request.path == '/metrics':
                values = {'phase_blocked': int(self.phase == 'BLOCKED'), 'phase_v1': int(self.phase == 'V1'), 'phase_v2': int(self.phase == 'V2'), 'holding': int(self.phase == 'HOLD'),
                          'inflight_v1': self.inflight['V1'], 'inflight_v2': self.inflight['V2'],
                          'waiters': self.waiters, 'wait_bytes': self.wait_bytes, 'aborts_total': self.aborts}
                return web.Response(text=self.metrics(values))
            data = await request.json()
            if data.get('run_id') != self.config['run_id']:
                raise ValueError('run mismatch')
            if request.path == '/begin':
                result = await self.begin()
            elif request.path == '/commit':
                result = await self.commit(data)
            elif request.path == '/abort':
                async with self.changed:
                    if data.get('hold_token') != self.token:
                        raise ValueError('hold token mismatch')
                    self.abort_locked('controller_abort')
                    result = self.status()
            else:
                raise ValueError('unknown control command')
            return web.json_response(result)
        except ValueError as error:
            return web.json_response({'error': str(error)}, status=409)


async def applications(config):
    gate = Gate(config)
    app = web.Application(client_max_size=150 * 1024 * 1024)
    app['gate'] = gate

    async def lifetime(application):
        from aiohttp import TCPConnector
        context = ssl.create_default_context(cafile=config.get('upstream_ca_file'))
        application['session'] = ClientSession(
            connector=TCPConnector(ssl=context),
            auto_decompress=False, cookie_jar=DummyCookieJar(), trust_env=False,
            timeout=ClientTimeout(total=None, connect=5, sock_read=600),
            skip_auto_headers={'User-Agent', 'Accept-Encoding', 'Content-Type'})
        # Pinned aiohttp also retries GET on a stale keepalive connection. OAuth callback
        # is a side-effecting GET, so disable that path as well; regression-tested below.
        application['session']._retry_connection = False
        yield
        if gate.watchdog:
            gate.watchdog.cancel()
        await application['session'].close()

    app.cleanup_ctx.append(lifetime)
    app.router.add_route('*', '/{path:.*}', gate.proxy)
    admin = web.Application(client_max_size=8192)
    admin.router.add_get('/status', gate.control)
    admin.router.add_get('/metrics', gate.control)
    for path in ('begin', 'abort', 'commit'):
        admin.router.add_post('/' + path, gate.control)
    return app, admin


async def serve(config, bind, port):
    app, admin = await applications(config)
    runner, control = web.AppRunner(app, access_log=None, shutdown_timeout=610), web.AppRunner(admin, access_log=None)
    await runner.setup()
    await control.setup()
    socket = Path(config['socket'])
    socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if socket.parent.stat().st_mode & 0o077:
        raise ValueError('private control directory required')
    if socket.exists():
        with sockets.socket(sockets.AF_UNIX) as probe:
            try:
                probe.connect(str(socket))
            except ConnectionRefusedError:
                socket.unlink()
            else:
                raise ValueError('another gate is already listening')
    await web.UnixSite(control, str(socket)).start()
    socket.chmod(0o600)
    await web.TCPSite(runner, bind, port).start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await control.cleanup()
        socket.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--bind', default='127.0.0.1', choices=('127.0.0.1', '0.0.0.0'))
    parser.add_argument('--port', type=int, default=19091)
    args = parser.parse_args()
    try:
        asyncio.run(serve(load(args.config), args.bind, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
