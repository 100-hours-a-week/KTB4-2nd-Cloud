#!/usr/bin/env python3
"""Smoke the deployed V1 refresh contract for one private synthetic session."""

import argparse
from http.cookies import SimpleCookie
import json
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import UUID


BASE = 'https://v1-staging.yeodam-2gether.com'
POOL_DIR = Path('/run/yeodam-synthetic-sessions')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    if str(UUID(args.run_id)) != args.run_id:
        raise ValueError('canonical run ID required')
    path = POOL_DIR / (args.run_id + '.json')
    if path.stat().st_mode & 0o077:
        raise ValueError('private session file permissions required')
    pool = json.loads(path.read_text())
    if pool.get('mode') != 'existing' or pool.get('runId') != args.run_id or len(pool['sessions']) != 1:
        raise ValueError('one existing-user session required')
    session = pool['sessions'][0]
    request = Request(BASE + '/api/auth/token/refresh', data=b'', method='POST', headers={
        'Cookie': 'refreshToken=' + session['refreshToken'] +
                  '; CSRF_CONTEXT=' + session['csrfContext'],
        'X-CSRF-TOKEN': session['csrfToken'],
    })
    with urlopen(request, timeout=15) as response:
        status = response.status
        cookies = SimpleCookie()
        for value in response.headers.get_all('Set-Cookie', []):
            cookies.load(value)
    access = cookies.get('accessToken')
    refresh = cookies.get('refreshToken')
    if status != 200 or not access or not refresh or not access.value or not refresh.value:
        raise RuntimeError('refresh did not return both cookies')
    me = Request(BASE + '/api/users/me', headers={
        'Cookie': 'accessToken=' + access.value + '; CSRF_CONTEXT=' + session['csrfContext'],
    })
    with urlopen(me, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError('refreshed access token was rejected')
    print('V1_REFRESH_OK status=200 cookies=2 users_me=200')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, RuntimeError) as error:
        print('V1_REFRESH_FAILED ' + type(error).__name__)
        raise SystemExit(1)
