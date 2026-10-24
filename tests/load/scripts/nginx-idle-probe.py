"""Observe bounded HTTP/1.1 idle connections. Default: print plan, never connect."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import http.client
import json
import math
from pathlib import Path
import re
import socket
import ssl
import threading
import time

ORIGIN = 'https://v1-staging.yeodam-2gether.com'
HOST = 'v1-staging.yeodam-2gether.com'
PATH = '/api/trips/map'


def now():
    return datetime.now(timezone.utc).isoformat()


def validate(args):
    if args.target != ORIGIN:
        raise ValueError('Only the exact registered V1 staging origin is allowed')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.run_id):
        raise ValueError('Explicit safe run ID required')
    for value, low, high in ((args.connections, 1, 64), (args.hold_seconds, 1, 90),
                             (args.open_interval, 0.05, 5), (args.request_timeout, 1, 10)):
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError('Outside preparation bounds: connections1..64, idle1..90s, interval0.05..5s, timeout1..10s')
    if args.execute and (args.confirm_target != ORIGIN or not args.output):
        raise ValueError('Execution requires matching confirmation and a new output file')
    return {'runId': args.run_id, 'target': ORIGIN, 'path': PATH,
            'protocol': 'HTTP/1.1', 'connections': args.connections,
            'holdSeconds': args.hold_seconds, 'openIntervalSeconds': args.open_interval,
            'requestTimeoutSeconds': args.request_timeout,
            'estimatedMaxSeconds': (args.connections - 1) * args.open_interval
                                   + 2 * args.request_timeout + args.hold_seconds + 2,
            'reconnect': False, 'executionEnabled': args.execute}


def run_probe(plan):
    if not plan['executionEnabled']:
        raise ValueError('Plan-only mode cannot open connections')
    context = ssl.create_default_context()
    context.set_alpn_protocols(['http/1.1'])
    stop = threading.Event()
    lock = threading.Lock()
    active = set()

    def interrupt(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def stop_all():
        stop.set()
        with lock:
            peers = list(active)
        for peer in peers:
            interrupt(peer)

    def one(index):
        row = {'index': index, 'startedAt': now(), 'httpConnected': False}
        connection = http.client.HTTPSConnection(HOST, timeout=plan['requestTimeoutSeconds'], context=context)
        sock = timer = None
        request_expired = threading.Event()
        try:
            if stop.is_set():
                row['outcome'] = 'not_started'
                return row
            connection.connect()
            sock = connection.sock
            with lock:
                active.add(sock)
            if stop.is_set():
                row['outcome'] = 'not_started'
                return row
            # Socket timeout alone is an inactivity limit. Also bound the entire
            # header/body phase so trickled responses cannot extend it forever.
            def expire_request():
                request_expired.set()
                interrupt(sock)

            timer = threading.Timer(plan['requestTimeoutSeconds'], expire_request)
            timer.start()
            connection.request('GET', PATH, headers={'Connection': 'keep-alive',
                                                      'User-Agent': 'yeodam-nginx-idle-probe'})
            response = connection.getresponse()
            row.update(status=response.status, protocol=f'HTTP/{response.version // 10}.{response.version % 10}')
            if response.version != 11 or response.status not in (401, 429):
                raise ValueError('Unexpected HTTP protocol/status')
            if len(response.read(65537)) > 65536:
                raise ValueError('Response body exceeds probe bound')
            timer.cancel()
            timer.join()
            if request_expired.is_set():
                raise TimeoutError('Request phase deadline reached')
            row['httpConnected'] = True
            row['responseCompleteAt'] = now()
            if response.will_close or connection.sock is None:
                row.update(outcome='server_closed_after_response', observedIdleSeconds=0)
            else:
                start = time.monotonic()
                sock.settimeout(plan['holdSeconds'])
                try:
                    extra = sock.recv(1)
                    if extra:
                        raise ValueError('Unexpected bytes after complete response')
                    row['outcome'] = 'server_closed_during_idle'
                except socket.timeout:
                    row['outcome'] = 'client_hold_limit_reached'
                row['observedIdleSeconds'] = time.monotonic() - start
        except Exception as error:
            row.update(outcome='error', errorType=type(error).__name__)
            stop_all()
        finally:
            if timer is not None:
                timer.cancel()
            if sock is not None:
                with lock:
                    active.discard(sock)
            connection.close()
            row['endedAt'] = now()
        return row

    started = now()
    rows = []
    with ThreadPoolExecutor(max_workers=plan['connections']) as executor:
        futures = []
        for index in range(plan['connections']):
            if stop.is_set():
                break
            futures.append(executor.submit(one, index))
            if index + 1 < plan['connections']:
                stop.wait(plan['openIntervalSeconds'])
        for future in as_completed(futures):
            rows.append(future.result())
    return {'schema': 1, 'plan': plan, 'startedAt': started, 'endedAt': now(),
            'complete': not stop.is_set(), 'attempted': len(rows),
            'httpResponsesCompleted': sum(row['httpConnected'] for row in rows),
            'rows': sorted(rows, key=lambda row: row['index']),
            'limits': ['Generator observations, not Nginx Active/Waiting or Host TCP counts.',
                       'No reconnect after server closes; no slow/incomplete headers.',
                       'A 401/429 response does not prove normal-user availability.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--connections', type=int, required=True)
    parser.add_argument('--hold-seconds', type=float, required=True)
    parser.add_argument('--open-interval', type=float, required=True)
    parser.add_argument('--request-timeout', type=float, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--confirm-target')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        plan = validate(args)
        if not args.execute:
            print(json.dumps(plan, indent=2))
            return 0
        # Refuse overwrite before any socket is opened. No implicit parent creation.
        with args.output.open('x', encoding='utf-8') as stream:
            result = run_probe(plan)
            json.dump(result, stream, indent=2)
            stream.write('\n')
        print(json.dumps({'output': str(args.output), 'complete': result['complete']}))
        return 0 if result['complete'] else 1
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
