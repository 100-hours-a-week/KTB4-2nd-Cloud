#!/usr/bin/env python3
"""One bounded authenticated HTTP/2 POST stage on the exact existing staging generator."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import time
from uuid import UUID

ROOT = Path('/opt/yeodam-load/nginx-h2-442')
RESULTS = Path('/var/lib/yeodam/nginx-h2-442')
ORIGIN = 'https://v1-staging.yeodam-2gether.com'
STAGES = {'smoke1': (1, 1, 1), 'small16': (16, 30, 64),
          'medium64': (64, 30, 256), 'instructor141': (141, 299, 0), 'instructor128': (128, 299, 0)}


def now(): return datetime.now(timezone.utc).isoformat()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--capture-id', required=True)
    p.add_argument('--run-id', required=True)
    p.add_argument('--stage', choices=STAGES, required=True)
    p.add_argument('--confirm-target', required=True)
    a = p.parse_args()
    UUID(a.capture_id);UUID(a.run_id)
    if a.confirm_target != ORIGIN or os.geteuid() != 0 or Path('/var/lib/cloud/data/instance-id').read_text().strip() != 'i-0c53b65eb35dd2b42':
        p.error('Exact existing V1 staging generator and origin only')
    os.umask(0o077)
    version = subprocess.check_output(['k6', 'version'], text=True).strip()
    if 'v2.3.0' not in version: p.error('Pinned k6 v2.3.0 required')
    pool = Path('/run/yeodam-nginx-h2-442/pool.json')
    if pool.stat().st_mode & 0o077: p.error('Private synthetic pool required')
    identity = json.loads(pool.read_text())
    vus, seconds, rate = STAGES[a.stage]
    if identity['baseUrl'] != ORIGIN or len(identity['sessions']) != 3 or (datetime.fromisoformat(identity['expiresAt'])-datetime.now(timezone.utc)).total_seconds() < seconds+150:
        p.error('Fresh three-owner synthetic pool required')
    folder = RESULTS/a.capture_id/a.stage
    folder.mkdir(parents=True, exist_ok=False)
    source = ROOT/'scenarios/nginx-h2-trip-replay.js'
    profile = ROOT/'fixtures/nginx-instructor-20261027-profile.json'
    env = {'PATH': os.environ['PATH'], 'K6_BASE_URL': ORIGIN, 'K6_H2_CONFIRM_TARGET': ORIGIN,
           'K6_H2_RUN_ID': a.run_id, 'K6_H2_VUS': str(vus), 'K6_H2_SECONDS': str(seconds),
           'K6_H2_RATE': str(rate), 'K6_H2_POOL': str(pool), 'K6_H2_PROFILE': str(profile)}
    plan = {'captureId': a.capture_id, 'runId': a.run_id, 'stage': a.stage, 'targetVus': vus,
            'seconds': seconds, 'targetRate': rate or 'recorded completion profile', 'startedAt': now(),
            'k6Version': version, 'sourceSha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'profileSha256': hashlib.sha256(profile.read_bytes()).hexdigest(),
            'syntheticUser': identity['sessions'][0]['userId'], 'originalBodyAndStreamsUnknown': True}
    del identity
    (folder/'plan.json').write_text(json.dumps(plan, indent=2))
    shutil.copyfile(source, folder/'source.js')
    shutil.copyfile(ROOT/'lib/nginx-h2-replay.mjs', folder/'source-lib.mjs')
    args = ['/usr/bin/time', '--verbose', '--output='+str(folder/'runner-time.txt'),
            'k6', 'run', '--no-color', '--log-format', 'raw', '--console-output', str(folder/'events.jsonl'),
            '--summary-export', str(folder/'native-summary.json'), str(source)]
    interrupted = False
    def stop(*_):
        nonlocal interrupted
        interrupted = True
    for sig in [signal.SIGTERM, signal.SIGINT]: signal.signal(sig, stop)
    with (folder/'run.log').open('w') as log, (folder/'generator.jsonl').open('w') as out:
        process = subprocess.Popen(args, env=env, stdout=log, stderr=log, start_new_session=True)
        start = time.monotonic();sent = False
        while process.poll() is None:
            processes = []
            for d in Path('/proc').iterdir():
                if not d.name.isdigit(): continue
                try:
                    if (d/'comm').read_text().strip() == 'k6':
                        values = (d/'stat').read_text().rsplit(')', 1)[1].split()
                        processes.append({'pid': int(d.name), 'ticks': int(values[11])+int(values[12]),
                                          'startTicks': int(values[19]), 'rssPages': int(values[21])})
                except (FileNotFoundError, ProcessLookupError): pass
            out.write(json.dumps({'utc': now(), 'elapsed': time.monotonic()-start, 'processes': processes,
                                  'hz': os.sysconf('SC_CLK_TCK'), 'pageBytes': os.sysconf('SC_PAGE_SIZE')})+'\n');out.flush()
            if not sent and (interrupted or (folder/'STOP').exists() or time.monotonic()-start > seconds+35):
                os.killpg(process.pid, signal.SIGINT);sent = True
            if time.monotonic()-start > seconds+45:
                os.killpg(process.pid, signal.SIGKILL)
            time.sleep(.5)
        code = process.wait()
    plan.update(endedAt=now(), exitCode=code, stopRequested=sent)
    (folder/'result.json').write_text(json.dumps(plan, indent=2))
    print(json.dumps(plan))
    return code


if __name__ == '__main__': raise SystemExit(main())
