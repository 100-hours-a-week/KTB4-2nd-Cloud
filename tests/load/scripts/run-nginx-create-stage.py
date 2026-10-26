#!/usr/bin/env python3
"""One bounded #428 k6 stage on the existing EC2 generator. Never repeats or cleans DB rows."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from uuid import UUID

ROOT = Path('/opt/yeodam-load/nginx-create-428')


def utc():
    return datetime.now(timezone.utc).isoformat()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id', required=True)
    p.add_argument('--mode', choices=['single', 'burst', 'rate'], default='rate')
    p.add_argument('--rate', type=int, choices=[1, 5, 10, 20, 30, 50, 75, 100], default=1)
    p.add_argument('--seconds', type=int, choices=[60, 120], default=60)
    p.add_argument('--session-count', type=int, choices=[1, 3], default=1)
    p.add_argument('--expect-rejection', action='store_true', help='Only the excess-load cohort may expect 201 or429')
    a = p.parse_args(); UUID(a.run_id); os.umask(0o077)
    if os.geteuid() != 0 or Path('/var/lib/cloud/data/instance-id').read_text().strip() != 'i-0c53b65eb35dd2b42':
        p.error('Exact existing staging generator root only')
    if os.environ.get('K6_HTTP_DEBUG'):
        p.error('Disable HTTP debug; credentials must not enter logs')
    version = subprocess.check_output(['k6', 'version'], text=True).strip()
    if 'v2.3.0' not in version:
        p.error('Pinned k6 v2.3.0 required')
    result = ROOT/'results'/a.run_id
    result.mkdir(parents=True, exist_ok=False)
    env = {**{k: v for k, v in os.environ.items() if not k.startswith('K6_')}, 'K6_BASE_URL': 'https://v1-staging.yeodam-2gether.com',
        'K6_CREATE_CONFIRM_TARGET': 'https://v1-staging.yeodam-2gether.com', 'K6_CREATE_RUN_ID': a.run_id,
        'K6_CREATE_MODE': a.mode, 'K6_CREATE_RPS': str(a.rate), 'K6_CREATE_SECONDS': str(a.seconds),
        'K6_CREATE_SESSION_COUNT': str(a.session_count), 'K6_CREATE_NO_REUSE': 'false',
        'K6_CREATE_EXPECT_REJECTION': 'true' if a.expect_rejection else 'false',
        'K6_SESSION_POOL_FILE': '/run/yeodam-nginx-create-428/create.json',
        'K6_READ_POOL_FILE': '/run/yeodam-nginx-create-428/read.json', 'K6_SUMMARY_PATH': str(result/'summary.json')}
    for key in ['K6_SESSION_POOL_FILE', 'K6_READ_POOL_FILE']:
        path = Path(env[key])
        if path.stat().st_mode & 0o077:
            p.error('Private session files required')
    plan = {'runId': a.run_id, 'mode': a.mode, 'rate': a.rate, 'seconds': a.seconds,
        'sessionCount': a.session_count, 'startedAt': utc(), 'k6Version': version,
        'sourceSha256': hashlib.sha256((ROOT/'scenarios/nginx-trip-create.js').read_bytes()).hexdigest(),
        'oneSecondGuardIsNotSlo': True, 'noRetryOrCleanup': True, 'expectRejection': a.expect_rejection}
    (result/'plan.json').write_text(json.dumps(plan, indent=2))
    args = ['/usr/bin/time', '--verbose', '--output='+str(result/'runner-time.txt'), 'k6', 'run',
        '--no-color', '--log-format', 'raw', '--console-output', str(result/'events.jsonl'),
        '--summary-export', str(result/'native-summary.json'), str(ROOT/'scenarios/nginx-trip-create.js')]
    with (result/'run.log').open('w') as log, (result/'generator.jsonl').open('w') as samples:
        process = subprocess.Popen(args, env=env, stdout=log, stderr=log, start_new_session=True)
        start = time.monotonic()
        stopped = False
        deadline = a.seconds + 30 if a.mode == 'rate' else 45
        while process.poll() is None:
            # Process cumulative ticks/RSS, not host load averages interpreted as CPU percentage.
            rows = []
            for d in Path('/proc').iterdir():
                if not d.name.isdigit():
                    continue
                try:
                    if (d/'comm').read_text().strip() == 'k6':
                        f = (d/'stat').read_text().rsplit(')', 1)[1].split()
                        rows.append({'pid': int(d.name), 'cpuTicks': int(f[11])+int(f[12]),
                                     'startTicks': int(f[19]), 'rssPages': int(f[21])})
                except (FileNotFoundError, ProcessLookupError):
                    continue
            samples.write(json.dumps({'utc': utc(), 'elapsedSeconds': time.monotonic()-start,
                'k6': rows, 'logicalCpus': os.cpu_count(), 'clockTicks': os.sysconf('SC_CLK_TCK'),
                'pageBytes': os.sysconf('SC_PAGE_SIZE')})+'\n'); samples.flush()
            if not stopped and ((result/'STOP').exists() or time.monotonic()-start > deadline):
                os.killpg(process.pid, signal.SIGINT); stopped = True
            if time.monotonic()-start > deadline+15:
                os.killpg(process.pid, signal.SIGKILL)
            time.sleep(.5)
        code = process.wait()
    report = {**plan, 'endedAt': utc(), 'exitCode': code, 'operatorOrDeadlineStop': stopped}
    (result/'result.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
