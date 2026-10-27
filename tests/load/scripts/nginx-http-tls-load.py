#!/usr/bin/env python3
"""Bounded #440 staging-only HTTP/1.1 reuse versus fresh full TLS 1.2 connections."""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import time

HOST = 'v1-staging.yeodam-2gether.com'
ORIGIN = 'https://' + HOST
GENERATOR = 'i-0c53b65eb35dd2b42'
spec = importlib.util.spec_from_file_location('tcp440', Path(__file__).with_name('nginx-tcp-capacity.py'))
tcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tcp)


def now():
    return datetime.now(timezone.utc).isoformat()


def validate(a):
    if a.confirm_target != ORIGIN or not 10 <= a.seconds <= 30:
        raise ValueError('Exact staging origin and 10..30 seconds only')
    if not 1 <= a.rate <= (450 if a.mode == 'reuse' else 20):
        raise ValueError('Reuse <=450 HTTP/s; fresh/tls-only <=20 connections/s')
    if not 1 <= a.workers <= 32:
        raise ValueError('1..32 client workers only')


def duration_summary(values):
    values = sorted(values)
    if not values:
        return None
    import math
    import statistics
    return {'count': len(values), 'median': statistics.median(values),
            'p95': values[math.ceil(.95 * len(values)) - 1], 'max': max(values)}


async def exercise(a):
    context = ssl.create_default_context()
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.options |= ssl.OP_NO_TICKET
    context.set_alpn_protocols(['http/1.1'])
    loop = asyncio.get_running_loop()
    dns_at = time.monotonic()
    addresses = await loop.getaddrinfo(HOST, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
    address = addresses[0][4][0]
    dns_ms = (time.monotonic() - dns_at) * 1000
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    queue = asyncio.Queue(maxsize=128)
    opened = negotiated = resumed = dropped = 0
    active = peak = 0
    statuses = Counter()
    errors = Counter()
    rows = []
    samples = []
    started_at = now()
    start = time.monotonic()
    cpu_start = time.process_time()
    end = start + a.seconds

    async def close(writer):
        nonlocal active
        if writer is not None:
            active -= 1
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), .5)
            except (OSError, asyncio.TimeoutError):
                pass

    async def worker(number):
        nonlocal opened, negotiated, resumed, active, peak
        reader = writer = None
        while True:
            item = await queue.get()
            if item is None:
                queue.task_done()
                break
            index, scheduled = item
            if stop.is_set():
                queue.task_done()
                continue
            at = time.monotonic()
            row = {'index': index, 'worker': number, 'utc': now(),
                   'queueDelayMs': (at-scheduled)*1000, 'newConnection': writer is None}
            phase = 'tcp'
            try:
                if writer is None:
                    connected_at = time.monotonic()
                    reader, writer = await asyncio.wait_for(asyncio.open_connection(address, 443), 3)
                    opened += 1
                    active += 1
                    peak = max(peak, active)
                    row['tcpMs'] = (time.monotonic()-connected_at)*1000
                    phase = 'tls'
                    tls_at = time.monotonic()
                    await asyncio.wait_for(writer.start_tls(context, server_hostname=HOST,
                                                           ssl_handshake_timeout=3), 3.2)
                    row['tlsMs'] = (time.monotonic()-tls_at)*1000
                    obj = writer.get_extra_info('ssl_object')
                    row.update(tlsVersion=obj.version(), sessionReused=obj.session_reused,
                               alpn=obj.selected_alpn_protocol(), cipher=obj.cipher()[0])
                    negotiated += 1
                    resumed += int(obj.session_reused)
                    if obj.session_reused or obj.version() != 'TLSv1.2' or row['alpn'] != 'http/1.1':
                        raise ValueError('Unexpected TLS/ALPN/resumption contract')
                if a.mode != 'tls-only':
                    phase = 'http'
                    request_at = time.monotonic()
                    writer.write(tcp.wire_request(close=a.mode == 'fresh'))
                    await writer.drain()
                    code, closed = await asyncio.wait_for(tcp.response(reader), 3)
                    row.update(status=code, httpMs=(time.monotonic()-request_at)*1000)
                    statuses[str(code)] += 1
                    if code not in (401, 429):
                        raise ValueError('Unexpected negative-read status')
                else:
                    closed = True
                row['ok'] = True
                if a.mode != 'reuse' or closed:
                    await close(writer)
                    reader = writer = None
            except Exception as error:
                row.update(ok=False, failureStage=phase, error=type(error).__name__)
                errors[phase+':'+type(error).__name__] += 1
                stop.set()  # Preserve first failure; no automatic retry or higher stage.
                await close(writer)
                reader = writer = None
            finally:
                row.update(endedAt=now(), operationMs=(time.monotonic()-at)*1000)
                rows.append(row)
                queue.task_done()
        await close(writer)

    async def sampler():
        while time.monotonic() < end and not stop.is_set():
            samples.append({'utc': now(), 'tcpOpened': opened, 'tlsCompleted': negotiated,
                            'responses': sum(statuses.values()), 'queueDepth': queue.qsize(),
                            'activeClientSockets': active,
                            'processCpuSeconds': time.process_time()-cpu_start})
            await asyncio.sleep(1)

    tasks = [asyncio.create_task(worker(i)) for i in range(a.workers)]
    sampling = asyncio.create_task(sampler())
    scheduled_count = a.rate * a.seconds
    for i in range(scheduled_count):
        due = start + i/a.rate
        await asyncio.sleep(max(0, due-time.monotonic()))
        if stop.is_set() or (a.stop_file and a.stop_file.exists()):
            stop.set()
            break
        if time.monotonic()-due > .1 or queue.full():
            dropped += 1
            continue
        queue.put_nowait((i, due))
    scheduled_end = time.monotonic()
    await queue.join()
    for _ in tasks:
        await queue.put(None)
    await asyncio.gather(*tasks)
    sampling.cancel()
    await asyncio.gather(sampling, return_exceptions=True)
    summaries = {key: duration_summary([r[key] for r in rows if key in r])
                 for key in ['tcpMs', 'tlsMs', 'httpMs', 'operationMs', 'queueDelayMs']}
    return {'schema': 1, 'startedAt': started_at, 'endedAt': now(), 'mode': a.mode,
            'target': ORIGIN, 'path': '/api/trips/map', 'address': address, 'dnsOnceMs': dns_ms,
            'httpVersion': '1.1', 'tlsVersion': 'TLSv1.2', 'clientSessionTickets': False,
            'opensslVersion': ssl.OPENSSL_VERSION, 'targetRate': a.rate, 'seconds': a.seconds,
            'workers': a.workers, 'scheduled': scheduled_count, 'attempted': len(rows),
            'notStarted': scheduled_count-dropped-len(rows), 'schedulerDropped': dropped,
            'scheduleElapsedSeconds': scheduled_end-start, 'elapsedSeconds': time.monotonic()-start,
            'processCpuSeconds': time.process_time()-cpu_start, 'tcpOpened': opened,
            'tlsCompleted': negotiated, 'tlsResumed': resumed, 'peakClientSockets': peak,
            'statuses': dict(statuses), 'errors': dict(errors), 'stopped': stop.is_set(),
            'timingsMs': summaries, 'rows': rows, 'samples': samples,
            'scope': 'Negative unauthenticated read; 401/429 are not normal-user successes; '
                     'TLS elapsed includes network, server queue, crypto and client scheduling'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode', choices=['reuse', 'fresh', 'tls-only'], required=True)
    p.add_argument('--rate', type=int, required=True)
    p.add_argument('--seconds', type=int, default=30)
    p.add_argument('--workers', type=int, default=4)
    p.add_argument('--confirm-target', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--stop-file', type=Path)
    p.add_argument('--execute', action='store_true')
    a = p.parse_args()
    try:
        validate(a)
    except ValueError as error:
        p.error(str(error))
    if not a.execute:
        print(json.dumps({'execute': False, 'mode': a.mode, 'rate': a.rate}));return
    if os.geteuid() != 0 or Path('/var/lib/cloud/data/instance-id').read_text().strip() != GENERATOR:
        p.error('Exact existing staging generator root only')
    os.umask(0o077)
    with a.output.open('x') as stream:
        result = asyncio.run(exercise(a))
        json.dump(result, stream)
    print(json.dumps({k: v for k, v in result.items() if k not in ('rows', 'samples')}))
    return 1 if result['stopped'] or result['errors'] or result['schedulerDropped'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
