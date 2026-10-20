#!/usr/bin/env python3
"""One real V1 Refresh then Logout for the isolated CDC authentication pilot."""
import argparse
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID


BASE = 'https://v1-staging.yeodam-2gether.com'
V2_BASE = 'https://staging.yeodam-2gether.com'


def request(path, cookies, csrf):
    headers = {'Cookie': '; '.join(f'{key}={value}' for key, value in cookies.items()),
               'X-CSRF-TOKEN': csrf}
    try:
        with urlopen(Request(BASE + path, data=b'', headers=headers, method='POST'), timeout=12) as response:
            status = response.status
            set_cookies = response.headers.get_all('Set-Cookie') or []
    except HTTPError as exc:
        raise RuntimeError(f'{path} returned HTTP {exc.code}') from None
    jar = SimpleCookie()
    for header in set_cookies:
        jar.load(header)
    return status, {name: morsel.value for name, morsel in jar.items()}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=('refresh', 'logout', 'v2-e2e'))
    p.add_argument('--pool', required=True)
    p.add_argument('--state', required=True)
    p.add_argument('--route-header', help='isolated ALB route marker for v2-e2e')
    a = p.parse_args()
    pool = Path(a.pool)
    state = Path(a.state)
    if pool.stat().st_mode & 0o077 or not str(pool).startswith('/run/yeodam-synthetic-sessions/'):
        raise ValueError('private synthetic session pool required')
    source = json.loads(pool.read_text())
    if len(source.get('sessions', [])) != 1:
        raise ValueError('one pilot session required')
    session = source['sessions'][0]
    if a.action == 'v2-e2e':
        if not a.route_header or str(UUID(a.route_header)) != a.route_header:
            raise ValueError('canonical isolated route marker required')
        # TLS uses the ALB certificate name. The dedicated listener rule uses
        # the V1 Host header plus this rehearsal marker; public V1 is untouched.
        def v2(path, cookies, csrf=None, method='GET'):
            headers = {
                'Host': 'v1-staging.yeodam-2gether.com',
                'X-Yeodam-A-Rehearsal': a.route_header,
                'Cookie': '; '.join(f'{key}={value}' for key, value in cookies.items()),
            }
            if csrf is not None:
                headers['X-CSRF-TOKEN'] = csrf
            try:
                with urlopen(Request(V2_BASE + path, data=b'' if method == 'POST' else None,
                                     headers=headers, method=method), timeout=12) as response:
                    return response.status, response.read(), response.headers.get_all('Set-Cookie') or []
            except HTTPError as exc:
                raise RuntimeError(f'isolated V2 {path} returned HTTP {exc.code}') from None

        status, _, _ = v2('/api/users/me', {'accessToken': session['accessToken']})
        if status != 200:
            raise RuntimeError('isolated V2 user lookup failed')
        print('AUTH368_V2_USERS_ME_HTTP_200')
        status, body, _ = v2('/api/auth/csrf', {
            'accessToken': session['accessToken'], 'CSRF_CONTEXT': session['csrfContext'],
        })
        token = json.loads(body)['data']['token']
        if status != 200 or not isinstance(token, str) or not token:
            raise RuntimeError('isolated V2 CSRF lookup failed')
        print('AUTH368_V2_CSRF_HTTP_200')
        status, _, set_cookies = v2('/api/auth/token/refresh', {
            'refreshToken': session['refreshToken'], 'CSRF_CONTEXT': session['csrfContext'],
        }, csrf=token, method='POST')
        jar = SimpleCookie()
        for header in set_cookies:
            jar.load(header)
        if status != 200 or 'accessToken' not in jar or 'refreshToken' not in jar:
            raise RuntimeError('isolated V2 Refresh did not return rotated cookies')
        print('AUTH368_V2_REFRESH_HTTP_200')
        return
    if a.action == 'refresh':
        if state.exists():
            raise FileExistsError('refuse to replace a completed Refresh state')
        status, changed = request('/api/auth/token/refresh',
                                  {'refreshToken': session['refreshToken'],
                                   'CSRF_CONTEXT': session['csrfContext']}, session['csrfToken'])
        if status != 200 or not changed.get('accessToken') or not changed.get('refreshToken'):
            raise RuntimeError('real V1 Refresh did not return both new tokens')
        payload = {'runId': source['runId'], 'sid': session['sid'],
                   'accessToken': changed['accessToken'],
                   'refreshToken': changed['refreshToken'],
                   'csrfContext': session['csrfContext'], 'csrfToken': session['csrfToken']}
        fd = os.open(state, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream, separators=(',', ':'))
        print('AUTH368_REFRESH_HTTP_200')
    else:
        if state.stat().st_mode & 0o077:
            raise ValueError('private Refresh state required')
        refreshed = json.loads(state.read_text())
        if refreshed['runId'] != source['runId'] or refreshed['sid'] != session['sid']:
            raise ValueError('Refresh state does not belong to pilot session')
        status, _ = request('/api/auth/logout',
                            {'accessToken': refreshed['accessToken'],
                             'CSRF_CONTEXT': refreshed['csrfContext']}, refreshed['csrfToken'])
        if status != 204:
            raise RuntimeError(f'real V1 Logout returned HTTP {status}')
        print('AUTH368_LOGOUT_HTTP_204')


if __name__ == '__main__':
    main()
