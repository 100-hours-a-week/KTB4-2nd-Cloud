#!/usr/bin/env python3
"""Issue/revoke one short session for the fixed #416 synthetic read fixture only."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
from uuid import uuid4

spec = importlib.util.spec_from_file_location('pool', Path(__file__).with_name('synthetic-session-pool.py'))
pool = importlib.util.module_from_spec(spec);spec.loader.exec_module(pool)
FIXTURE = 'df3d52be-79e0-4984-8cf5-b7ae1d73ffd8'
USER = 11007
BASE = Path('/run/yeodam-nginx-read-fixture')


def revoke(path, run_id):
    data = json.loads(path.read_text())
    if data.get('runId') != run_id or len(data['sessions']) != 1:
        raise ValueError('Pool identity differs')
    s = data['sessions'][0]
    if s['userId'] != USER:
        raise ValueError('Only the synthetic fixture owner is allowed')
    sid = pool.sql_literal(s['sid'])
    context = pool.sql_literal(hashlib.sha256(s['csrfContext'].encode()).hexdigest())
    pool.run_mysql('START TRANSACTION; DELETE FROM csrf_tokens WHERE browser_context_hash='+context+
                   '; DELETE FROM login_sessions WHERE user_id='+str(USER)+' AND sid='+sid+'; COMMIT;')
    if pool.run_mysql('SELECT COUNT(*) FROM login_sessions WHERE user_id='+str(USER)+' AND sid='+sid+';') != '0':
        raise ValueError('Session revocation failed')
    path.unlink()
    print(json.dumps({'revoked': True, 'remainingSessionsForSid': 0, 'privateFileDeleted': True}))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['issue', 'revoke'])
    p.add_argument('--run-id', type=pool.run_uuid, required=True)
    a = p.parse_args();os.umask(0o077)
    if os.geteuid() != 0 or Path('/var/lib/cloud/data/instance-id').read_text().strip() != 'i-05d320e3a87200d5a':
        raise ValueError('V1 staging App root only')
    expected = 'mig-'+FIXTURE+'-0000@staging.invalid'
    if pool.run_mysql('SELECT email FROM users WHERE user_id='+str(USER)+';') != expected:
        raise ValueError('Fixture owner changed; refuse session access')
    BASE.mkdir(mode=0o700, exist_ok=True)
    if BASE.stat().st_mode & 0o077:
        raise ValueError('Private output directory required')
    output = BASE/(a.run_id+'.json')
    if a.mode == 'revoke':
        revoke(output, a.run_id);return
    if output.exists() or pool.run_mysql('SELECT @@read_only,@@super_read_only;') != '0\t0':
        raise ValueError('Pool already exists or source is fenced')
    secret = pool.parameter(pool.SECRET_PARAMETER);issuer = pool.parameter(pool.ISSUER_PARAMETER)
    if len(secret.encode()) < 32 or not issuer.startswith('https://'):
        raise ValueError('Unexpected staging JWT contract')
    sid = str(uuid4());now = datetime.now(timezone.utc)
    s = {'userId': USER, 'sid': sid, 'csrfContext': sid,
         'accessToken': pool.access_token(USER,sid,secret,issuer,now), 'csrfToken': None}
    pool.save_existing_pool(output,a.run_id,FIXTURE,now,[s])
    try:
        refresh_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        pool.run_mysql('INSERT INTO login_sessions (sid,user_id,refresh_token_hash,expires_at) VALUES ('+
                       pool.sql_literal(sid)+','+str(USER)+','+pool.sql_literal(refresh_hash)+
                       ',DATE_ADD(NOW(6), INTERVAL 30 MINUTE));')
        s['csrfToken'] = pool.http_session(s['accessToken'],sid)
        output.write_text(json.dumps({'version':1,'mode':'existing','runId':a.run_id,'datasetId':FIXTURE,
                                     'baseUrl':pool.BASE_URL,'expiresAt':(now+pool.TOKEN_TTL).isoformat(),
                                     'sessions':[s]}))
    except Exception:
        revoke(output,a.run_id);raise
    print(json.dumps({'privateFile':str(output),'userId':USER,'expiresAt':(now+pool.TOKEN_TTL).isoformat()}))


if __name__ == '__main__':
    main()
