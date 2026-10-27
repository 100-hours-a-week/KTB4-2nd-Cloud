#!/usr/bin/env python3
"""Bounded V1 staging ENA observation and fresh external DNS/TCP/TLS/HTTP probes."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import tempfile
import time
from uuid import UUID

ORIGIN = 'https://v1-staging.yeodam-2gether.com'
APP = 'i-05d320e3a87200d5a'
GENERATOR = 'i-0c53b65eb35dd2b42'
ENA_KEYS = ['bw_in_allowance_exceeded', 'bw_out_allowance_exceeded',
            'pps_allowance_exceeded', 'conntrack_allowance_exceeded',
            'linklocal_allowance_exceeded', 'conntrack_allowance_available']


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_ena(text):
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r'\s*([a-z0-9_]+):\s*([0-9]+)\s*', line)
        if match and match[1] in ENA_KEYS:
            result[match[1]] = int(match[2])
    if not all(key in result for key in ENA_KEYS[:5]):
        raise ValueError('Required ENA allowance counters missing')
    return result


def counter_delta(current, previous):
    if previous is None:
        return None
    # An ENA/driver reset cannot be interpreted as a negative packet count.
    if any(current[k] < previous.get(k, current[k]) for k in ENA_KEYS[:5]):
        return None
    return {k: current[k]-previous[k] for k in ENA_KEYS[:5]}


def failure_stage(exit_code, timings):
    if exit_code == 0:
        return 'http'
    if exit_code in (1, 2, 3, 4, 5, 23, 26, 27, 48):
        return 'probe-tool'  # Unsupported local curl features are not server failures.
    if exit_code == 6:
        return 'dns'
    if exit_code == 7:
        return 'tcp'
    if exit_code in (35, 51, 58, 59, 60, 77, 83, 90):
        return 'tls'
    if exit_code == 28:
        if timings.get('tls', 0) > 0:
            return 'http-timeout'
        if timings.get('tcp', 0) > 0:
            return 'tls-timeout'
        return 'dns-or-tcp-timeout'  # getaddrinfo timing alone cannot distinguish these.
    return 'transport-other'


def probe(path, curl='curl'):
    # A new curl process means a fresh TCP/TLS connection, no browser cookies or credentials.
    with tempfile.TemporaryDirectory(prefix='nginx438-header-') as folder:
        headers = Path(folder)/'headers'
        fmt = json.dumps({'status': '%{http_code}', 'dns': '%{time_namelookup}',
                          'tcp': '%{time_connect}', 'tls': '%{time_appconnect}',
                          'ttfb': '%{time_starttransfer}', 'total': '%{time_total}',
                          'httpVersion': '%{http_version}', 'remoteIp': '%{remote_ip}'})
        started = now()
        result = subprocess.run([curl, '-q', '--noproxy', '*', '--http1.1',
            '--connect-timeout', '3', '--max-time', '5', '--max-redirs', '0',
            '-sS', '-D', str(headers), '-o', os.devnull, '-w', fmt,
            '-H', 'User-Agent: yeodam-external438', ORIGIN+path],
            capture_output=True, text=True, timeout=7)
        data = json.loads(result.stdout)
        for key in ['dns', 'tcp', 'tls', 'ttfb', 'total']:
            data[key] = float(data[key])
        data['status'] = int(data['status'])
        ids = re.findall(r'^x-request-id:\s*([a-zA-Z0-9_-]{1,80})\s*$',
                         headers.read_text(errors='replace') if headers.exists() else '', re.I | re.M)
        # No response body, cookie, auth header or full header file is retained.
        return {'utc': started, 'endedAt': now(), 'path': path, 'exitCode': result.returncode,
                'stage': failure_stage(result.returncode, data), 'timings': data,
                'requestId': ids[-1] if ids else None,
                'ok': result.returncode == 0 and data['status'] == 200 and data['total'] <= 2,
                'stderr': result.stderr[:500]}


def validate(a):
    UUID(a.run_id)
    if a.confirm_target != ORIGIN or not 10 <= a.seconds <= 1800:
        raise ValueError('Exact V1 staging origin and duration 10..1800 seconds required')
    if a.mode == 'ena':
        if os.geteuid() != 0 or Path('/var/lib/cloud/data/instance-id').read_text().strip() != APP:
            raise ValueError('ENA observation is restricted to exact staging App root')
    elif platform.system() != 'Darwin':
        if os.geteuid() != 0 or Path('/var/lib/cloud/data/instance-id').read_text().strip() != GENERATOR:
            raise ValueError('External probe requires Mac or explicit existing VPC generator')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode', choices=['ena', 'probe'], required=True)
    p.add_argument('--run-id', required=True)
    p.add_argument('--seconds', type=int, required=True)
    p.add_argument('--confirm-target', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--stop-file', type=Path)
    p.add_argument('--execute', action='store_true')
    a = p.parse_args()
    try:
        validate(a)
    except (ValueError, OSError) as error:
        p.error(str(error))
    if not a.execute:
        print(json.dumps({'execute': False, 'mode': a.mode, 'seconds': a.seconds}))
        return
    os.umask(0o077)
    a.output.mkdir(parents=True, exist_ok=False)
    stopped = False
    def stop(*_):
        nonlocal stopped
        stopped = True
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, stop)
    began = now()
    start = time.monotonic()
    index = errors = failures = consecutive = 0
    previous = first = latest = None
    stages = Counter()
    state = {'runId': a.run_id, 'mode': a.mode, 'startedAt': began, 'sampleCount': 0,
             'errors': 0, 'failures': 0, 'guardTriggered': False}
    with (a.output/'samples.jsonl').open('x') as stream:
        while not stopped and time.monotonic()-start < a.seconds and not (a.output/'STOP').exists():
            at = time.monotonic()
            row = {'utc': now()}
            try:
                if a.mode == 'ena':
                    result = subprocess.run(['ethtool', '-S', 'ens5'], check=True,
                                            capture_output=True, text=True, timeout=.8)
                    latest = parse_ena(result.stdout)
                    row.update(counters=latest, delta=counter_delta(latest, previous))
                    row['resetOrInitial'] = row['delta'] is None
                    if first is None:
                        first = latest
                    previous = latest
                else:
                    row = probe(['/login', '/api/actuator/health'][index % 2])
                    stages[row['stage']] += 1
                    if not row['ok']:
                        failures += 1
                        consecutive += 1
                    else:
                        consecutive = 0
                    if consecutive >= 3:
                        # A local supervisor must stop the detached generator. Keep observing recovery.
                        (a.output/'GUARD').touch(mode=0o600)
                        if a.stop_file:
                            a.stop_file.touch(mode=0o600)
            except Exception as error:
                errors += 1
                row['collectionError'] = type(error).__name__
                if a.mode == 'probe':
                    (a.output/'GUARD').touch(mode=0o600)
                    if a.stop_file:
                        a.stop_file.touch(mode=0o600)
            row['sampleSeconds'] = round(time.monotonic()-at, 4)
            stream.write(json.dumps(row)+'\n');stream.flush()
            index += 1
            state = {'runId': a.run_id, 'mode': a.mode, 'startedAt': began, 'updatedAt': now(),
                     'sampleCount': index, 'errors': errors, 'failures': failures,
                     'consecutiveFailures': consecutive, 'guardTriggered': (a.output/'GUARD').exists(),
                     'stages': dict(stages), 'firstCounters': first, 'lastCounters': latest,
                     'counterDelta': counter_delta(latest, first) if latest else None}
            temp = a.output/'checkpoint.tmp';temp.write_text(json.dumps(state));temp.replace(a.output/'checkpoint.json')
            interval = 1 if a.mode == 'ena' else 2
            until = min(start+a.seconds, at+interval)
            while not stopped and time.monotonic() < until and not (a.output/'STOP').exists():
                time.sleep(min(.1, max(0, until-time.monotonic())))
    state.update(endedAt=now(), stopped=stopped or (a.output/'STOP').exists(), complete=not errors,
                 scope='New observation only; never attribute earlier cumulative ENA counters to this run')
    (a.output/'summary.json').write_text(json.dumps(state, indent=2))
    print(json.dumps(state))


if __name__ == '__main__':
    main()
