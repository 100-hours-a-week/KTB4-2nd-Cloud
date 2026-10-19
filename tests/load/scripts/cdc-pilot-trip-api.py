#!/usr/bin/env python3
"""One actual POST /api/trips from the V1 staging App; never prints credentials."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

BASE_URL = 'https://v1-staging.yeodam-2gether.com'
POOL_DIR = Path('/run/yeodam-synthetic-sessions')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--sequence', type=int, default=0)
    parser.add_argument('--name-prefix', choices=('b304', 'b309'), default='b304')
    args = parser.parse_args()
    if str(UUID(args.run_id)) != args.run_id:
        raise ValueError('canonical run ID required')
    if not 0 <= args.sequence <= 9:
        raise ValueError('pilot sequence out of range')
    path = POOL_DIR / (args.run_id + '.json')
    if path.stat().st_mode & 0o077:
        raise ValueError('private session file permissions required')
    pool = json.loads(path.read_text())
    if pool.get('mode') != 'existing' or pool.get('runId') != args.run_id or len(pool.get('sessions', [])) != 1:
        raise ValueError('unexpected session pool')
    expiry = datetime.fromisoformat(pool['expiresAt'])
    remaining_seconds = (expiry - datetime.now(timezone.utc)).total_seconds()
    if remaining_seconds <= 120:
        raise ValueError('session expires too soon')
    print(f'API_SESSION_REMAINING_SECONDS {int(remaining_seconds)}', flush=True)
    session = pool['sessions'][0]
    name_key = args.run_id if args.sequence == 0 else args.run_id + ':' + str(args.sequence)
    trip_name = args.name_prefix + hashlib.sha256(name_key.encode()).hexdigest()[:6]
    payload = {'tripName': trip_name, 'startDate': '2025-01-01',
               'endDate': '2025-01-03', 'regionCodes': ['50110']}
    request = Request(BASE_URL + '/api/trips',
                      data=json.dumps(payload).encode(), method='POST',
                      headers={'Content-Type': 'application/json',
                               'Cookie': 'accessToken=' + session['accessToken'] +
                                         '; CSRF_CONTEXT=' + session['csrfContext'],
                               'X-CSRF-TOKEN': session['csrfToken']})
    try:
        with urlopen(request, timeout=10) as response:
            body = json.load(response)
            status = response.status
    except HTTPError as error:
        print('API_RESULT status=' + str(error.code) + ' run_id=' + args.run_id, flush=True)
        raise SystemExit(1)
    except URLError:
        print('API_RESULT transport_error run_id=' + args.run_id, flush=True)
        raise SystemExit(1)
    trip_id = body.get('data', {}).get('tripId')
    state = body.get('data', {}).get('status')
    print(f'API_RESULT status={status} run_id={args.run_id} user_id={session["userId"]} '
          f'trip_name={trip_name} trip_id={trip_id} state={state}', flush=True)
    if status != 201 or not isinstance(trip_id, int) or state != 'PROCESSING':
        raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, TypeError) as error:
        print('API_PRECHECK_FAILED ' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
