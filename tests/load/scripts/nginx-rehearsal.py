#!/usr/bin/env python3
"""Detached, bounded evidence capture on the exact V1 staging App. No load or policy changes."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from uuid import UUID

spec = importlib.util.spec_from_file_location('observer', Path(__file__).with_name('nginx-connection-observer.py'))
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)
BASE = Path('/var/lib/yeodam/nginx-rehearsal')
SITE = Path('/etc/nginx/sites-enabled/yeodam-v1-staging')
INSTANCE = 'i-05d320e3a87200d5a'
MAX_BYTES = 512 * 1024 * 1024
PHASES = ['baseline', 'load-start', 'load-end', 'recovery-end', 'warning']


def utc():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(data, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class LogFollower:
    """Drain the old open inode before following a replacement. Source files stay untouched."""
    def __init__(self, source, output):
        self.source, self.output = Path(source), Path(output)
        self.output.touch(mode=0o600, exist_ok=False)
        self.stream = self.source.open('rb')
        self.stream.seek(0, 2)
        self.initial = {'inode': os.fstat(self.stream.fileno()).st_ino, 'offset': self.stream.tell()}
        self.rotations = 0
        self.truncated = False
        self.bytes = 0

    def drain(self, budget, maximum=2 * 1024 * 1024):
        size = os.fstat(self.stream.fileno()).st_size
        if size < self.stream.tell():
            self.truncated = True
            self.stream.seek(0)
        data = self.stream.read(min(maximum, budget))
        if data:
            with self.output.open('ab') as out:
                out.write(data)
            self.bytes += len(data)
        # An open inode survives rename/compression. Do not discard an unread tail.
        if self.stream.tell() == os.fstat(self.stream.fileno()).st_size:
            current = self.source.stat()
            if current.st_ino != os.fstat(self.stream.fileno()).st_ino:
                self.stream.close()
                self.stream = self.source.open('rb')
                self.rotations += 1
        return len(data)

    def state(self):
        stat = os.fstat(self.stream.fileno())
        return {'initial': self.initial, 'inode': stat.st_ino, 'offset': self.stream.tell(),
                'unreadBytes': max(0, stat.st_size-self.stream.tell()), 'capturedBytes': self.bytes,
                'rotations': self.rotations, 'copyTruncateDetected': self.truncated}

    def close(self):
        self.stream.close()


def resources():
    memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    total = int(memory['MemTotal'].split()[0])
    available = int(memory['MemAvailable'].split()[0])
    fds = {}
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():
            continue
        try:
            if (p/'comm').read_text().strip() == 'nginx':
                fds[p.name] = len(list((p/'fd').iterdir()))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return {'memoryUsedPercent': round((1-available/total)*100, 3),
            'memoryAvailableKiB': available, 'nginxFds': fds,
            'nginxFdTotal': sum(fds.values()), 'loadAverage': os.getloadavg()}


def collect(root, seconds):
    plan = json.loads((root/'plan.json').read_text())
    followers = {name: LogFollower('/var/log/nginx/'+filename, root/(name+'.private.raw'))
                 for name, filename in [('access', 'yeodam-access.json'), ('error', 'yeodam-error.log')]}
    stopped = False
    def terminate(*_):
        nonlocal stopped
        stopped = True
    for sig in [signal.SIGTERM, signal.SIGINT]:
        signal.signal(sig, terminate)
    start = time.monotonic()
    started = utc()
    before = observer.cpu()
    count = 0
    errors = 0
    written = 0
    maximum_lag = 0
    reason = 'duration'
    try:
        with (root/'samples.jsonl').open('x') as output:
            for index in range(seconds):
                time.sleep(max(0, start+index-time.monotonic()))
                lag = max(0, time.monotonic()-start-index)
                maximum_lag = max(maximum_lag, lag)
                row = {'utc': utc(), 'monotonicSeconds': time.monotonic()-start, 'schedulingLagSeconds': lag}
                try:
                    _, _, _, body = observer.request('/stub_status', loopback=True)
                    row.update(observer.parse_stub(body.decode()))
                    row.update(resources())
                    after = observer.cpu()
                    delta = after[0]-before[0]
                    row['cpuPercent'] = None if index == 0 or not delta else round(100*(1-(after[1]-before[1])/delta), 3)
                    before = after
                    row['httpsEstablished'] = len(subprocess.check_output(
                        ['ss', '-Htn', 'state', 'established', 'sport', '=', ':443'], text=True, timeout=2).splitlines())
                    disk = shutil.disk_usage('/')
                    row['diskPercent'] = round(disk.used/disk.total*100, 3)
                    if row['diskPercent'] >= 80:
                        reason = 'disk-limit'
                        stopped = True
                    for follower in followers.values():
                        if written >= MAX_BYTES:
                            reason = 'archive-budget'
                            stopped = True
                            break
                        written += follower.drain(MAX_BYTES-written)
                except Exception as error:
                    errors += 1
                    row['errorType'] = type(error).__name__
                output.write(json.dumps(row)+'\n')
                output.flush()
                if index % 5 == 0:
                    os.fsync(output.fileno())
                count += 1
                checkpoint = {'runId': plan['runId'], 'startedAt': started, 'updatedAt': utc(),
                              'sampleCount': count, 'sampleErrors': errors, 'maximumSchedulingLagSeconds': maximum_lag,
                              'logs': {k: v.state() for k, v in followers.items()}, 'pid': os.getpid()}
                save(root/'checkpoint.json', checkpoint)
                if stopped or (root/'STOP').exists():
                    if reason == 'duration':
                        reason = 'requested-stop' if (root/'STOP').exists() else 'signal'
                    break
        # One bounded final drain; a remaining backlog is reported, never hidden.
        for follower in followers.values():
            written += follower.drain(max(0, MAX_BYTES-written))
        logs = {k: v.state() for k, v in followers.items()}
        complete = (reason in ['duration', 'requested-stop'] and not errors
                    and all(not s['unreadBytes'] and not s['copyTruncateDetected'] for s in logs.values()))
        save(root/'result.json', {**checkpoint, 'endedAt': utc(), 'reason': reason, 'logs': logs,
             'collectionComplete': complete,
             'siteUnchanged': hashlib.sha256(SITE.read_bytes()).hexdigest() == plan['siteSha256'],
             'precisionWarning': maximum_lag > 1,
             'scope': 'Host observation only; no assertion of normal-user success or attacker request loss'})
    finally:
        for follower in followers.values():
            follower.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['start', '_collect', 'status', 'mark', 'stop', 'normal-start'])
    p.add_argument('--run-id', required=True)
    p.add_argument('--seconds', type=int, default=1800)
    p.add_argument('--expected-site-sha256')
    p.add_argument('--phase', choices=PHASES)
    p.add_argument('--session-run-id')
    a = p.parse_args()
    UUID(a.run_id)
    os.umask(0o077)
    if os.geteuid() != 0 or Path('/var/lib/cloud/data/instance-id').read_text().strip() != INSTANCE:
        p.error('Root on exact V1 staging App only')
    root = BASE/a.run_id
    unit = 'yeodam-nginx426-'+a.run_id
    if a.mode == 'start':
        if not 10 <= a.seconds <= 3600:
            p.error('Duration must be10..3600 seconds')
        sha = hashlib.sha256(SITE.read_bytes()).hexdigest()
        if sha != a.expected_site_sha256:
            p.error('Inspect the current site before capture')
        if shutil.disk_usage('/').used/shutil.disk_usage('/').total >= .8:
            p.error('Disk already exceeds80%')
        root.mkdir(parents=True, exist_ok=False)
        save(root/'plan.json', {'schema': 1, 'runId': a.run_id, 'requestedAt': utc(), 'seconds': a.seconds,
             'siteSha256': sha, 'instanceId': INSTANCE, 'unit': unit, 'archiveBudgetBytes': MAX_BYTES,
             'sourceSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
        (root/'marks.jsonl').touch(mode=0o600)
        subprocess.run(['systemd-run', '--quiet', '--collect', '--unit='+unit,
            '--property=RuntimeMaxSec='+str(a.seconds+30), '--property=Nice=10',
            '--property=MemoryMax=128M', '--property=CPUQuota=20%',
            'python3', str(Path(__file__).resolve()), '_collect', '--run-id', a.run_id,
            '--seconds', str(a.seconds)], check=True)
        print(json.dumps({'runId': a.run_id, 'unit': unit, 'requested': True, 'root': str(root)}))
    elif a.mode == 'normal-start':
        if (root/'result.json').exists() or (root/'STOP').exists():
            p.error('Normal probes require an active capture')
        UUID(a.session_run_id)
        plan = json.loads((root/'plan.json').read_text())
        if not 10 <= a.seconds <= 1500:
            p.error('Normal probe duration10..1500s; prepare a fresh30-minute fixture session')
        subprocess.run(['systemd-run', '--quiet', '--collect', '--unit='+unit+'-normal',
            '--property=RuntimeMaxSec='+str(a.seconds+30), '--property=Nice=10',
            '--property=MemoryMax=128M', '--property=CPUQuota=20%', 'python3',
            str(Path(__file__).with_name('nginx-connection-observer.py')), '--experiment', '426',
            '--normal-only', '--run-id', a.run_id, '--session-run-id', a.session_run_id,
            '--expected-site-sha256', plan['siteSha256'], '--seconds', str(a.seconds)], check=True)
        print(json.dumps({'normalProbeRequested': True, 'runId': a.run_id}))
    elif a.mode == '_collect':
        collect(root, a.seconds)
    elif a.mode == 'mark':
        if not a.phase or not (root/'checkpoint.json').exists() or (root/'result.json').exists():
            p.error('An active capture and phase are required')
        with (root/'marks.jsonl').open('a') as f:
            item = {'phase': a.phase, 'utc': utc()}
            f.write(json.dumps(item)+'\n')
            f.flush()
            os.fsync(f.fileno())
        print(json.dumps(item))
    elif a.mode == 'stop':
        if not (root/'plan.json').exists():
            p.error('Unknown capture')
        (root/'STOP').touch(mode=0o600)
        normal = Path('/var/lib/yeodam/nginx-connections-426')/a.run_id
        if normal.exists():
            (normal/'STOP').touch(mode=0o600)
        print(json.dumps({'stopRequested': True, 'runId': a.run_id}))
    else:
        state = next((root/n for n in ['result.json', 'checkpoint.json', 'plan.json'] if (root/n).exists()), None)
        if not state:
            p.error('Unknown capture')
        data = json.loads(state.read_text())
        normal = Path('/var/lib/yeodam/nginx-connections-426')/a.run_id/'summary.json'
        data['normalResult'] = json.loads(normal.read_text()) if normal.exists() else None
        print(json.dumps(data))


if __name__ == '__main__':
    main()
