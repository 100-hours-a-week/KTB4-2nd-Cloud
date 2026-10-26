#!/usr/bin/env python3
"""Three new staging synthetic owners; short sessions only. Revoke credentials, retain all trip data."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
from types import SimpleNamespace
from uuid import uuid4

spec = importlib.util.spec_from_file_location('pool', Path(__file__).with_name('synthetic-session-pool.py'))
pool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pool)
BASE = Path('/run/yeodam-nginx-create-428')


def owned(data, run):
    if data.get('runId') != run or len(data.get('sessions', [])) != 3:
        raise ValueError('Exact three-owner pool required')
    for i, s in enumerate(data['sessions']):
        expected = 'mig-'+run+'-'+str(i).zfill(4)+'@staging.invalid'
        if pool.run_mysql('SELECT email FROM users WHERE user_id='+str(int(s['userId']))+';') != expected:
            raise ValueError('Synthetic owner differs; refuse')


def revoke_sessions(sessions):
    for s in sessions:
        pool.run_mysql('START TRANSACTION; DELETE FROM csrf_tokens WHERE browser_context_hash='
            +pool.sql_literal(hashlib.sha256(s['csrfContext'].encode()).hexdigest())
            +'; DELETE FROM login_sessions WHERE user_id='+str(int(s['userId']))+' AND sid='
            +pool.sql_literal(s['sid'])+'; COMMIT;')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['issue', 'renew', 'revoke'])
    p.add_argument('--run-id', required=True, type=pool.run_uuid)
    a = p.parse_args(); os.umask(0o077)
    if os.geteuid() != 0 or Path('/var/lib/cloud/data/instance-id').read_text().strip() != 'i-05d320e3a87200d5a':
        p.error('Exact V1 staging App root required')
    if pool.run_mysql('SELECT @@read_only,@@super_read_only;') != '0\t0':
        p.error('Source fenced; do not change sessions')
    BASE.mkdir(mode=0o700, exist_ok=True)
    if BASE.stat().st_mode & 0o077:
        p.error('Private directory required')
    path = BASE/(a.run_id+'.json')
    if a.mode == 'issue':
        pool.prepare(SimpleNamespace(run_id=a.run_id, count=3, output=str(path)))
    data = json.loads(path.read_text()); owned(data, a.run_id)
    if a.mode == 'revoke':
        revoke_sessions(data['sessions'])
        for s in data['sessions']:
            if pool.run_mysql('SELECT COUNT(*) FROM login_sessions WHERE user_id='+str(s['userId'])+' AND sid='+pool.sql_literal(s['sid'])+';') != '0':
                raise ValueError('SID still present')
        path.unlink(); print(json.dumps({'revoked': True, 'sessions': 3, 'allTripDataRetained': True})); return
    if a.mode == 'renew':
        now = datetime.now(timezone.utc)
        secret, issuer = pool.parameter(pool.SECRET_PARAMETER), pool.parameter(pool.ISSUER_PARAMETER)
        fresh = []
        try:
            for old in data['sessions']:
                sid = str(uuid4()); user = int(old['userId'])
                pool.run_mysql('INSERT INTO login_sessions (sid,user_id,refresh_token_hash,expires_at) VALUES ('
                    +pool.sql_literal(sid)+','+str(user)+','+pool.sql_literal(hashlib.sha256(secrets.token_bytes(32)).hexdigest())
                    +',DATE_ADD(NOW(6),INTERVAL 30 MINUTE));')
                s = {'userId': user, 'sid': sid, 'csrfContext': sid,
                     'accessToken': pool.access_token(user, sid, secret, issuer, now)}
                fresh.append(s)
                s['csrfToken'] = pool.http_session(s['accessToken'], sid)
            pool.save_existing_pool(path, a.run_id, a.run_id, now, fresh)
        except Exception:
            revoke_sessions(fresh); raise
        revoke_sessions(data['sessions'])
        data = json.loads(path.read_text())
    for s in data['sessions']:
        user = str(int(s['userId']))
        pool.run_mysql('INSERT INTO user_stats (user_id) SELECT '+user+' WHERE NOT EXISTS (SELECT 1 FROM user_stats WHERE user_id='+user+');'
            +' UPDATE login_sessions SET expires_at=DATE_ADD(NOW(6),INTERVAL 30 MINUTE) WHERE user_id='+user+' AND sid='+pool.sql_literal(s['sid'])+';')
    print(json.dumps({'privateFile': str(path), 'syntheticUsers': [s['userId'] for s in data['sessions']],
        'expiresAt': data['expiresAt'], 'mode': a.mode, 'tripDataReset': False}))


if __name__ == '__main__':
    main()
