#!/usr/bin/env python3
"""Bounded same-peer read-limit experiment. No configuration changes or writes."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import threading
import time
from uuid import UUID, uuid4

HOST = 'v1-staging.yeodam-2gether.com'
ORIGIN = 'https://' + HOST
ROUTE = '/api/trips'
INSTANCES = {'generator': 'i-0c53b65eb35dd2b42', 'control': 'i-05d320e3a87200d5a'}
SECONDS, LOAD_START, LOAD_END, RATE = 90, 25, 55, 20
NORMAL_OFFSETS = (.025, .075, .125, .175)
ROOT = Path('/var/lib/yeodam/nginx-shared-ip456')


def utc():
    return datetime.now(timezone.utc).isoformat()


def phase(elapsed):
    return 'baseline' if elapsed < LOAD_START else 'shared-load' if elapsed < LOAD_END else 'recovery'


def guard(role, cohort, status, duration_ms, elapsed):
    if duration_ms > 1000 or status == 0 or status >= 500:
        return True
    if cohort == 'load':
        return status not in (401, 429)
    if role == 'control':
        return status != 200
    # A normal 429 is the measured outcome, never a successful normal response.
    permitted = (200, 429) if LOAD_START <= elapsed < LOAD_END + 10 else (200,)
    return status not in permitted


def load_session(path, run_id):
    if path.is_symlink() or path.stat().st_mode & 0o077 or path.parent.stat().st_mode & 0o077:
        raise ValueError('Private regular session file and directory required')
    value = json.loads(path.read_text())
    sessions = value.get('sessions', [])
    if (value.get('runId') != run_id or value.get('baseUrl') != ORIGIN or len(sessions) != 1
            or sessions[0].get('userId') != 11007):
        raise ValueError('Exact synthetic fixture and session run required')
    remaining = datetime.fromisoformat(value['expiresAt']) - datetime.now(timezone.utc)
    if remaining.total_seconds() < SECONDS + 120:
        raise ValueError('Synthetic session window too short')
    token = sessions[0].get('accessToken', '')
    if len(token.split('.')) != 3:
        raise ValueError('Expected fixture access token')
    return token


def competing_load():
    for path in Path('/proc').iterdir():
        if path.name.isdigit():
            try:
                if (path / 'comm').read_text().strip() in {'k6', 'h2load', 'wrk', 'vegeta'}:
                    return True
            except OSError:
                pass
    return False


class Client:
    def __init__(self, run_id, role, cohort, token=None):
        self.connection = None
        self.headers = {'User-Agent': 'yeodam-shared-ip456/' + role + '/' + cohort}
        if token:
            self.headers['Cookie'] = 'accessToken=' + token

    def close(self):
        if self.connection:
            self.connection.close()
            self.connection = None

    def request(self):
        started, at, probe_id = time.monotonic(), utc(), str(uuid4())
        code, rid, error = 0, None, None
        try:
            if self.connection is None:
                self.connection = http.client.HTTPSConnection(HOST, timeout=2,
                                                              context=ssl.create_default_context())
            # Existing Nginx access logging retains this header even when a 429
            # response omits X-Request-ID. It is a per-request nonce, never auth.
            self.connection.request('GET', ROUTE,
                                    headers={**self.headers, 'X-Migration-Run-Id': probe_id})
            response = self.connection.getresponse()
            code = response.status
            candidate = response.getheader('X-Request-ID', '')
            rid = candidate if re.fullmatch('[a-f0-9]{32}', candidate) else None
            if len(response.read(2_000_001)) > 2_000_000:
                raise ValueError('Response budget exceeded')
            if response.will_close:
                self.close()
        except Exception as exc:
            # No exception text, request headers, response bodies or credentials in evidence.
            error = type(exc).__name__
            self.close()
        return {'at': at, 'endedAt': utc(), 'status': code, 'requestId': rid, 'probeId': probe_id,
                'durationMs': round(1000 * (time.monotonic() - started), 3), 'errorType': error,
                'method': 'GET', 'route': ROUTE, 'protocol': 'HTTP/1.1'}


def preflight(client):
    try:
        return client.request()
    finally:
        # The start rendezvous can exceed Nginx's 15s idle timeout.
        client.close()


def run(args):
    os.umask(0o077)
    UUID(args.run_id)
    UUID(args.session_run_id)
    if not args.execute or os.geteuid() != 0:
        raise ValueError('Explicit execution and exact instance root required')
    if Path('/var/lib/cloud/data/instance-id').read_text().strip() != INSTANCES[args.role]:
        raise ValueError('Unexpected instance')
    resolved = {x[4][0] for x in socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)}
    if not resolved or not resolved <= {'3.37.13.241', '10.30.0.174'}:
        raise ValueError('Original staging Nginx resolution required; no clone override')
    start_at = datetime.fromisoformat(args.start_at.replace('Z', '+00:00'))
    if start_at.tzinfo is None or not 5 <= start_at.timestamp() - time.time() <= 60:
        raise ValueError('Common UTC start must be 5..60 seconds ahead')
    if competing_load():
        raise ValueError('Another load tool is active; refuse shared generator use')
    token = load_session(args.pool_file, args.session_run_id)
    output = ROOT / args.run_id / args.role
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    plan = {'runId': args.run_id, 'role': args.role, 'startAt': start_at.isoformat(),
            'seconds': SECONDS, 'normalRate': 1, 'loadRate': RATE if args.role == 'generator' else 0,
            'loadStartSecond': LOAD_START, 'loadEndSecond': LOAD_END, 'origin': ORIGIN,
            'route': ROUTE, 'protocol': 'HTTP/1.1', 'automaticRetries': False,
            'loadAuthenticated': False, 'normalAuthenticated': True, 'configurationChanged': False}
    plan['normalOffsetsSeconds'] = list(NORMAL_OFFSETS)
    plan['normalScheduler'] = 'Independent thread; one request per second with fixed rotating phase'
    (output / 'plan.json').write_text(json.dumps(plan, indent=2))
    stop, lock = threading.Event(), threading.Lock()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    counts, clients, dropped = Counter(), [], 0
    tls = threading.local()
    normal = Client(args.run_id, args.role, 'normal', token)
    begun = utc()
    with (output / 'requests.jsonl').open('x') as stream:
        def emit(cohort, scheduled):
            if cohort == 'normal':
                client = normal
            else:
                if not hasattr(tls, 'client'):
                    tls.client = Client(args.run_id, args.role, 'load')
                    with lock:
                        clients.append(tls.client)
                client = tls.client
            row = client.request()
            row.update(runId=args.run_id, role=args.role, cohort=cohort,
                       scheduledSecond=round(scheduled, 3), phase=phase(scheduled))
            invalid = bool(row['errorType']) or guard(
                args.role, cohort, row['status'], row['durationMs'], scheduled)
            row['guardTriggered'] = invalid
            with lock:
                counts[(row['phase'], cohort, str(row['status']))] += 1
                stream.write(json.dumps(row) + '\n')
                stream.flush()
                temporary = output / 'checkpoint.tmp'
                temporary.write_text(json.dumps({'runId': args.run_id, 'role': args.role,
                    'updatedAt': utc(), 'requestCount': sum(counts.values()), 'guardTriggered': stop.is_set() or invalid}))
                temporary.replace(output / 'checkpoint.json')
            if invalid:
                stop.set()

        # Preflight is retained, but does not contribute to phase response counts.
        row = preflight(normal)
        (output / 'preflight.json').write_text(json.dumps(row, indent=2))
        if row['status'] != 200 or row['errorType'] or not row['requestId']:
            stop.set()
        while time.time() < start_at.timestamp() and not stop.is_set():
            time.sleep(.05)
        epoch = time.monotonic() - max(0, time.time() - start_at.timestamp())
        slots = threading.BoundedSemaphore(4)

        def worker(scheduled):
            try:
                emit('load', scheduled)
            finally:
                slots.release()

        def normal_worker():
            nonlocal dropped
            for second in range(SECONDS):
                scheduled = second + NORMAL_OFFSETS[second % len(NORMAL_OFFSETS)]
                if stop.wait(max(0, epoch + scheduled - time.monotonic())) or (output / 'STOP').exists():
                    return
                if competing_load():
                    stop.set()
                    return
                if time.monotonic() - (epoch + scheduled) > .5:
                    with lock:
                        dropped += 1
                    stop.set()
                    return
                emit('normal', scheduled)

        normal_thread = threading.Thread(target=normal_worker)
        normal_thread.start()
        try:
            with ThreadPoolExecutor(max_workers=4) as executor:
                for tick in range(SECONDS * RATE):
                    scheduled = tick / RATE
                    if stop.is_set() or (output / 'STOP').exists():
                        break
                    time.sleep(max(0, epoch + scheduled - time.monotonic()))
                    if args.role == 'generator' and LOAD_START <= scheduled < LOAD_END and not stop.is_set():
                        if time.monotonic() - (epoch + scheduled) > .5:
                            dropped += 1
                            stop.set()
                            break
                        if slots.acquire(blocking=False):
                            executor.submit(worker, scheduled)
                        else:
                            dropped += 1
                            stop.set()
                            break
        finally:
            normal_thread.join(timeout=3)
            normal.close()
            for client in clients:
                client.close()
    expected = SECONDS + (RATE * (LOAD_END - LOAD_START) if args.role == 'generator' else 0)
    summary = {'runId': args.run_id, 'role': args.role, 'startedAt': begun, 'endedAt': utc(),
               'complete': not stop.is_set() and not (output / 'STOP').exists() and sum(counts.values()) == expected,
               'guardTriggered': stop.is_set(), 'dropped': dropped, 'expectedRequests': expected,
               'requestCount': sum(counts.values()), 'counts': [{'phase': p, 'cohort': c, 'status': s, 'count': n}
               for (p, c, s), n in sorted(counts.items())],
               'credentialsOwnedByIssuer': True, 'credentialsRevoked': False,
               'scope': 'Normal 429 is impact, not normal success. No browser/OAuth or historical caller attribution.'}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary))
    return 0 if summary['complete'] else 1


def analyze(generator, control, access, probe_map=()):
    index = defaultdict(list)
    for row in access:
        index[row.get('request_id')].append(row)
    probes = defaultdict(list)
    for row in probe_map:
        probes[row.get('probeId')].append(row.get('request_id'))
    proof, failures, sources, seen = [], [], defaultdict(set), set()
    for role, rows in [('generator', generator), ('control', control)]:
        for row in rows:
            response_rid = row.get('requestId')
            probe = row.get('probeId')
            tagged = probes.get(probe, []) if probe else []
            tag_valid = (not probe or (response_rid is not None and not tagged) or
                         (len(tagged) == 1 and (response_rid is None or response_rid == tagged[0])))
            rid = response_rid or (tagged[0] if len(tagged) == 1 else None)
            matches = index.get(rid, []) if rid else []
            duplicate = rid in seen
            seen.add(rid)
            valid = (len(matches) == 1 and not duplicate and bool(rid) and tag_valid
                     and matches[0].get('status') == row.get('status')
                     and matches[0].get('method') == 'GET'
                     and matches[0].get('normalized_route') == ROUTE
                     and re.fullmatch('[a-f0-9]{16}', str(matches[0].get('sourceId', ''))) is not None)
            if valid:
                sources[(role, row['cohort'])].add(matches[0].get('sourceId'))
            else:
                failures.append({'role': role, 'requestId': rid, 'matches': len(matches), 'duplicate': duplicate})
            proof.append({'role': role, 'cohort': row['cohort'], 'phase': row['phase'],
                          'status': row['status'], 'requestId': rid, 'matched': valid,
                          'responseRequestId': response_rid, 'probeId': probe,
                          'limitStatus': matches[0].get('limit_req_status') if valid else None,
                          'upstreamStatus': matches[0].get('upstream_status') if valid else None})
    normal = sources[('generator', 'normal')]
    load = sources[('generator', 'load')]
    other = sources[('control', 'normal')]
    same_peer = len(normal) == len(load) == 1 and normal == load
    different_peer = len(other) == 1 and len(normal) == 1 and other != normal
    affected = [x for x in proof if x['role'] == 'generator' and x['cohort'] == 'normal'
                and x['phase'] == 'shared-load' and x['status'] == 429 and x['matched']
                and x['limitStatus'] == 'REJECTED' and x['upstreamStatus'] in ('', '-', None)]
    control_ok = bool(control) and all(x['status'] == 200 and not x.get('errorType') for x in control)
    baseline_ok = bool(generator) and all(x['status'] == 200 for x in generator
        if x['cohort'] == 'normal' and x['phase'] == 'baseline')
    tail = [x for x in generator if x['cohort'] == 'normal' and x['scheduledSecond'] >= LOAD_END + 10]
    recovery_ok = bool(tail) and all(x['status'] == 200 for x in tail)
    def complete(rows, role):
        normal_ticks = [x['scheduledSecond'] for x in rows if x['cohort'] == 'normal']
        load_ticks = [round(x['scheduledSecond'] * RATE) for x in rows if x['cohort'] == 'load']
        expected_load = list(range(LOAD_START * RATE, LOAD_END * RATE)) if role == 'generator' else []
        return (sorted(int(t) for t in normal_ticks) == list(range(SECONDS)) and sorted(load_ticks) == expected_load
                and all(x.get('role') == role and x['phase'] == phase(x['scheduledSecond'])
                        and not x.get('errorType') and not x.get('guardTriggered') for x in rows))
    run_ids = {x.get('runId') for x in generator + control}
    timelines_complete = complete(generator, 'generator') and complete(control, 'control') and len(run_ids) == 1 and None not in run_ids
    return {'requestMatchesComplete': not failures, 'correlationFailures': failures,
            'timelinesComplete': timelines_complete,
            'samePeerVerified': same_peer, 'differentPeerControlVerified': different_peer,
            'baselineNormalAll200': baseline_ok, 'independentNormalAll200': control_ok,
            'steadyRecoveryAll200': recovery_ok, 'affectedNormalRequests': len(affected),
            'sharedIpNormal429Demonstrated': bool(affected) and same_peer and different_peer and not failures
                and baseline_ok and control_ok and recovery_ok and timelines_complete,
            'requests': proof, 'historicalUserImpactProven': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    trial = sub.add_parser('run')
    trial.add_argument('--role', choices=INSTANCES, required=True)
    trial.add_argument('--run-id', required=True)
    trial.add_argument('--session-run-id', required=True)
    trial.add_argument('--pool-file', type=Path, required=True)
    trial.add_argument('--start-at', required=True)
    trial.add_argument('--execute', action='store_true')
    report = sub.add_parser('analyze')
    for name in ['generator', 'control', 'access', 'output']:
        report.add_argument('--' + name, type=Path, required=True)
    report.add_argument('--probe-map', type=Path)
    a = p.parse_args()
    try:
        if a.action == 'run':
            return run(a)
        def rows(path):
            return [json.loads(x) for x in path.read_text().splitlines() if x]
        result = analyze(rows(a.generator), rows(a.control), rows(a.access),
                         rows(a.probe_map) if a.probe_map else [])
        fd = os.open(a.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(result, out, indent=2)
        print(json.dumps({k: v for k, v in result.items() if k != 'requests'}))
        return 0
    except Exception as exc:
        p.exit(1, 'Trial refused or incomplete: ' + type(exc).__name__ + '; no credentials printed.\n')


if __name__ == '__main__':
    raise SystemExit(main())
