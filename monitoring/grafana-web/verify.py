#!/usr/bin/env python3
"""Check the public login boundary with an ephemeral Viewer; never print credentials."""
import argparse
import base64
import http.cookiejar
import json
from pathlib import Path
import secrets
import urllib.error
import urllib.request

HOSTS = {'staging': 'grafana-staging.yeodam-2gether.com', 'production': 'grafana.yeodam-2gether.com'}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--environment', choices=HOSTS, required=True)
    args = p.parse_args()
    url = 'https://' + HOSTS[args.environment]
    password = Path('/etc/yeodam/plg/admin-password').read_text().strip()
    auth = 'Basic ' + base64.b64encode(('admin:' + password).encode()).decode()
    def admin(path, body=None, method=None):
        req = urllib.request.Request('http://127.0.0.1:3000' + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': auth, 'Content-Type': 'application/json'}, method=method)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or '{}')
    def call(path, opener=None, body=None):
        req = urllib.request.Request(url + path,
              data=None if body is None else json.dumps(body).encode(),
              headers={'Content-Type': 'application/json'})
        try:
            with (opener.open(req, timeout=20) if opener else urllib.request.urlopen(req, timeout=20)) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, b'', {}
    assert call('/api/search')[0] == 401, 'Unauthenticated dashboard API must reject access'
    assert call('/metrics')[0] == 404, 'Internal Grafana metrics must not be public'
    assert call('/%6detrics')[0] in (400, 404), 'Encoded metrics path must not bypass the boundary'
    assert call('/api/admin/users')[0] == 401, 'Unauthenticated admin API must reject access'
    assert call('/login', body={'user': 'plg375-invalid', 'password': secrets.token_urlsafe(36)})[0] == 401
    login = 'plg375-check-' + secrets.token_hex(4)
    user_password = secrets.token_urlsafe(40)
    created = admin('/api/admin/users', {'name': 'Temporary access verification', 'login': login,
                    'password': user_password, 'OrgId': 1}, 'POST')
    uid = created['id']
    try:
        # Explicitly verify Viewer role; do not rely on the server default.
        admin('/api/org/users/' + str(uid), {'role': 'Viewer'}, 'PATCH')
        cookies = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
        status, _, headers = call('/login', opener, {'user': login, 'password': user_password})
        assert status == 200, 'Viewer login failed'
        session = [c for c in cookies if c.name == 'grafana_session']
        assert session and session[0].secure, 'Session cookie must be Secure'
        assert any(k.lower() == 'httponly' for k in session[0]._rest), 'Session cookie must be HttpOnly'
        assert call('/api/dashboards/uid/yeodam-v2-backend', opener)[0] == 200
        assert call('/api/users', opener)[0] == 403, 'Viewer must not administer users'
        datasource = args.environment + '-prometheus'
        status, payload, _ = call('/api/ds/query', opener, {
            'from': 'now-5m', 'to': 'now', 'queries': [{'refId': 'A',
            'datasource': {'type': 'prometheus', 'uid': datasource},
            'expr': 'sum(up)', 'instant': True, 'range': False}]})
        assert status == 200 and 'error' not in json.loads(payload)['results']['A']
        print(json.dumps({'environment': args.environment, 'url': url,
            'unauthenticated_data': 401, 'public_metrics': 404,
            'viewer_dashboard': 200, 'viewer_query': 200, 'viewer_admin': 403,
            'secure_session_cookie': True, 'temporary_user_id': uid}))
    finally:
        admin('/api/admin/users/' + str(uid), method='DELETE')


if __name__ == '__main__':
    main()
