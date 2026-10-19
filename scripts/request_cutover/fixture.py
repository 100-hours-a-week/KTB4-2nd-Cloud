"""Run-scoped synthetic identities and real-image continuity smoke, no k6 load."""
import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import ssl
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout, CookieJar, TCPConnector
from yarl import URL

from .config import load, private_file
from .control import database


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def jwt(uid, sid):
    def enc(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).rstrip(b'=')
    now = int(time.time())
    body = b'.'.join([enc({'alg': 'HS256', 'typ': 'JWT'}), enc({
        'iss': 'https://yeodam-2gether.com', 'aud': ['yeodam-api'], 'iat': now,
        'exp': now + 1800, 'sub': str(uid), 'sid': sid})])
    sig = base64.urlsafe_b64encode(hmac.new(os.environ['CUTOVER_JWT_SECRET'].encode(),
                                         body, hashlib.sha256).digest()).rstrip(b'=')
    return (body + b'.' + sig).decode()


def seed(config, output, count):
    if not 1 <= count <= 100 or output.exists():
        raise ValueError('new private pool and count 1..100 required')
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.parent.stat().st_mode & 0o077:
        raise ValueError('fixture directory must be private')
    sessions = []
    with database(config) as db:
        with db.cursor() as cursor:
            for index in range(count):
                uid, sid, context = 900311 + index, str(uuid4()), str(uuid4())
                refresh, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                email = f'cutover-{config["run_id"]}-{index}@staging.invalid'
                cursor.execute('INSERT INTO users(user_id,email,nickname) VALUES(%s,%s,%s)',
                               (uid, email, '전환' + str(index)))
                cursor.execute('INSERT INTO user_stats(user_id) VALUES(%s)', (uid,))
                cursor.execute('INSERT INTO consents(user_id,is_agreed,agreed_at) VALUES(%s,1,NOW(6))', (uid,))
                cursor.execute('''INSERT INTO oauth_accounts(user_id,provider,provider_user_id)
                    VALUES(%s,'KAKAO',%s)''', (uid, str(uid)))
                cursor.execute('''INSERT INTO login_sessions(sid,user_id,refresh_token_hash,expires_at)
                    VALUES(%s,%s,%s,DATE_ADD(NOW(6),INTERVAL 2 HOUR))''', (sid, uid, digest(refresh)))
                cursor.execute('''INSERT INTO csrf_tokens(browser_context_hash,token_value,expires_at)
                    VALUES(%s,%s,DATE_ADD(NOW(6),INTERVAL 1 DAY))''', (digest(context), csrf))
                sessions.append({'userId': uid, 'sid': sid, 'accessToken': jwt(uid, sid),
                    'refreshToken': refresh, 'csrfContext': context, 'csrfToken': csrf})
            ticket, profile, state = (secrets.token_urlsafe(32) for _ in range(3))
            cursor.execute('''INSERT INTO login_tickets(ticket_hash,browser_context_hash,
                provider_user_id,email,expires_at) VALUES(%s,%s,%s,%s,DATE_ADD(NOW(6),INTERVAL 10 MINUTE))''',
                (digest(ticket), digest(sessions[0]['csrfContext']), str(sessions[0]['userId']),
                 f'cutover-{config["run_id"]}-0@staging.invalid'))
            cursor.execute('''INSERT INTO profile_tokens(token_hash,provider_user_id,email,expires_at)
                VALUES(%s,%s,%s,DATE_ADD(NOW(6),INTERVAL 10 MINUTE))''',
                (digest(profile), 'new-' + config['run_id'], 'new-' + config['run_id'] + '@staging.invalid'))
            cursor.execute('''INSERT INTO oauth_states(state_hash,browser_context_hash,expires_at)
                VALUES(%s,%s,DATE_ADD(NOW(6),INTERVAL 5 MINUTE))''',
                (digest(state), digest(sessions[0]['csrfContext'])))
    private_file(output, json.dumps({'runId': config['run_id'], 'issuedAt': int(time.time()),
        'sessions': sessions, 'loginTicket': ticket, 'profileToken': profile, 'oauthState': state}) + '\n')
    return {'synthetic_sessions': len(sessions), 'run_id': config['run_id']}


async def smoke(config, pool, base, expected, index=0, rotate=False):
    if pool['runId'] != config['run_id']:
        raise ValueError('pool belongs to a different run')
    row = pool['sessions'][index]
    jar = CookieJar(unsafe=True)
    jar.update_cookies({'accessToken': row['accessToken'], 'refreshToken': row['refreshToken'],
                        'CSRF_CONTEXT': row['csrfContext']}, response_url=URL(base))
    connector = TCPConnector(ssl=ssl.create_default_context(cafile=config.get('upstream_ca_file')))
    async with ClientSession(connector=connector, cookie_jar=jar,
                             headers={'Host': config['public_host']},
                             timeout=ClientTimeout(total=15)) as client:
        async def request(method, path, status, **kwargs):
            async with client.request(method, base + '/api' + path, allow_redirects=False,
                server_hostname='yeodam-2gether.com', **kwargs) as response:
                if response.status != status:
                    raise ValueError(f'smoke {method} {path} status {response.status}, expected {status}')
                data = await response.json() if response.content_type == 'application/json' else None
                return data, response.headers
        _, headers = await request('GET', '/users/me', 200)
        if headers.get('X-Yeodam-Cutover-Target') not in (expected, None):
            raise ValueError('request reached an unexpected version')
        csrf, _ = await request('GET', '/auth/csrf', 200)
        if csrf['data']['token'] != row['csrfToken']:
            raise ValueError('CSRF value was not preserved')
        if rotate:
            before = jar.filter_cookies(URL(base + '/api/auth'))['refreshToken'].value
            await request('POST', '/auth/token/refresh', 200,
                          headers={'X-CSRF-TOKEN': row['csrfToken']})
            after = jar.filter_cookies(URL(base + '/api/auth'))['refreshToken'].value
            if after == before:
                raise ValueError('Refresh did not rotate')
            await request('GET', '/users/me', 200)
            await request('POST', '/auth/logout', 204,
                          headers={'X-CSRF-TOKEN': row['csrfToken']})
            # Server clears browser cookies. Reuse the original JWT to test revocation, not absence.
            jar.update_cookies({'accessToken': row['accessToken']}, response_url=URL(base))
            await request('GET', '/users/me', 401)
    return {'existing_login': True, 'csrf_preserved': True, 'version': expected,
            'refresh_and_revocation': rotate}


async def oauth_smoke(config, pool, base):
    """Consume migrated values through the actual V2 image, without Kakao traffic."""
    jar = CookieJar(unsafe=True)
    jar.update_cookies({'OAUTH_BROWSER_CONTEXT': pool['sessions'][0]['csrfContext'],
                        'profileToken': pool['profileToken']}, response_url=URL(base))
    connector = TCPConnector(ssl=ssl.create_default_context(cafile=config.get('upstream_ca_file')))
    async with ClientSession(connector=connector, cookie_jar=jar,
                             headers={'Host': config['public_host']},
                             timeout=ClientTimeout(total=15)) as client:
        async def req(method, path, **kw):
            async with client.request(method, base + '/api' + path, allow_redirects=False,
                server_hostname='yeodam-2gether.com', **kw) as response:
                data = await response.json() if response.content_type == 'application/json' else None
                return response.status, data, response.headers
        # Blank authorization code consumes a valid state, then fails before calling Kakao.
        for expected in ('KAKAO_AUTHENTICATION_FAILED', 'OAUTH_STATE_INVALID_OR_EXPIRED'):
            status, _, heads = await req('GET', '/auth/kakao/callback?state=' + pool['oauthState'])
            if status != 302 or parse_qs(urlsplit(heads['Location']).query).get('error') != [expected]:
                raise ValueError('migrated OAuth state consumption/replay failed')
        _, data, _ = await req('GET', '/auth/csrf')
        csrf = data['data']['token']
        status, _, _ = await req('POST', '/users/me/profile',
            json={'nickname': '새전환'}, headers={'X-CSRF-TOKEN': csrf})
        if status != 201:
            raise ValueError('migrated profile token registration failed: ' + str(status))
        # Profile cookie is cleared. Explicit replay of the consumed original must fail.
        jar.update_cookies({'profileToken': pool['profileToken']}, response_url=URL(base))
        _, data, _ = await req('GET', '/auth/csrf')
        status, _, _ = await req('POST', '/users/me/profile', json={'nickname': '새전환'},
                                headers={'X-CSRF-TOKEN': data['data']['token']})
        if status != 401:
            raise ValueError('consumed profile token was not rejected')
        _, data, _ = await req('GET', '/auth/csrf')
        status, _, _ = await req('POST', '/auth/token/exchange', json={'loginTicket': pool['loginTicket']},
                                headers={'X-CSRF-TOKEN': data['data']['token']})
        if status != 200:
            raise ValueError('migrated login ticket exchange failed: ' + str(status))
        _, data, _ = await req('GET', '/auth/csrf')
        status, _, _ = await req('POST', '/auth/token/exchange', json={'loginTicket': pool['loginTicket']},
                                headers={'X-CSRF-TOKEN': data['data']['token']})
        if status != 401:
            raise ValueError('consumed login ticket was not rejected')
    return {'oauth_state_once': True, 'profile_registration_once': True, 'login_ticket_once': True,
            'external_oauth_called': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('seed', 'before', 'after', 'verify', 'oauth'))
    p.add_argument('--config', required=True)
    p.add_argument('--pool', required=True, type=Path)
    p.add_argument('--count', type=int, default=10)
    p.add_argument('--base', default='https://ingress')
    p.add_argument('--session-index', type=int, default=0)
    args = p.parse_args()
    config = load(args.config)
    if args.command == 'seed':
        result = seed(config, args.pool, args.count)
    else:
        if args.pool.stat().st_mode & 0o077:
            raise ValueError('pool file must be private')
        if args.command == 'oauth':
            print(json.dumps(asyncio.run(oauth_smoke(config, json.loads(args.pool.read_text()), args.base))))
            return
        result = asyncio.run(smoke(config, json.loads(args.pool.read_text()), args.base,
                                   'V1' if args.command == 'before' else 'V2',
                                   args.session_index, args.command == 'after'))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
