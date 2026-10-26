#!/usr/bin/env python3
"""Upload test users need the V1 registration statistics row; retain uploaded data."""
import argparse
from datetime import datetime, timezone
import secrets
from uuid import uuid4
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('pool', Path(__file__).with_name('synthetic-session-pool.py'))
pool = importlib.util.module_from_spec(spec);spec.loader.exec_module(pool)
BASE = Path('/run/yeodam-nginx-upload-test')


def identity(path, run_id):
    data = json.loads(path.read_text())
    if data.get('runId') != run_id or len(data.get('sessions', [])) != 1:
        raise ValueError('Unexpected private pool identity')
    s = data['sessions'][0];user = int(s['userId'])
    email = 'mig-'+run_id+'-0000@staging.invalid'
    if pool.run_mysql('SELECT email FROM users WHERE user_id='+str(user)+';') != email:
        raise ValueError('Only the exact synthetic upload owner is allowed')
    return user,s


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['issue','initialize-stats','renew','revoke'])
    p.add_argument('--run-id', required=True, type=pool.run_uuid)
    a=p.parse_args();os.umask(0o077)
    if os.geteuid()!=0 or Path('/var/lib/cloud/data/instance-id').read_text().strip()!='i-05d320e3a87200d5a':
        raise ValueError('V1 staging App root only')
    BASE.mkdir(mode=0o700,exist_ok=True)
    if BASE.stat().st_mode & 0o077:raise ValueError('Private output directory required')
    path=BASE/(a.run_id+'.json')
    if a.mode=='issue':
        pool.prepare(SimpleNamespace(run_id=a.run_id,count=1,output=str(path)))
    user,s=identity(path,a.run_id)
    if a.mode=='renew':
        if pool.run_mysql('SELECT @@read_only,@@super_read_only;')!='0\t0':raise ValueError('Source is fenced')
        old=path.read_bytes();now=datetime.now(timezone.utc);sid=str(uuid4())
        secret=pool.parameter(pool.SECRET_PARAMETER);issuer=pool.parameter(pool.ISSUER_PARAMETER)
        if len(secret.encode())<32 or not issuer.startswith('https://'):raise ValueError('Unexpected JWT contract')
        fresh={'userId':user,'sid':sid,'csrfContext':sid,'accessToken':pool.access_token(user,sid,secret,issuer,now),'csrfToken':None}
        sid_sql=pool.sql_literal(sid)
        try:
            rh=pool.sql_literal(hashlib.sha256(secrets.token_bytes(32)).hexdigest())
            pool.run_mysql('INSERT INTO login_sessions (sid,user_id,refresh_token_hash,expires_at) VALUES ('+sid_sql+','+str(user)+','+rh+',DATE_ADD(NOW(6), INTERVAL 30 MINUTE));')
            fresh['csrfToken']=pool.http_session(fresh['accessToken'],sid)
            pool.save_existing_pool(path,a.run_id,a.run_id,now,[fresh])
        except Exception:
            pool.run_mysql('DELETE FROM csrf_tokens WHERE browser_context_hash='+pool.sql_literal(hashlib.sha256(sid.encode()).hexdigest())+'; DELETE FROM login_sessions WHERE user_id='+str(user)+' AND sid='+sid_sql+';')
            path.write_bytes(old);raise
        pool.run_mysql('DELETE FROM csrf_tokens WHERE browser_context_hash='+pool.sql_literal(hashlib.sha256(s['csrfContext'].encode()).hexdigest())+'; DELETE FROM login_sessions WHERE user_id='+str(user)+' AND sid='+pool.sql_literal(s['sid'])+';')
        print(json.dumps({'renewed':True,'syntheticUserId':user,'expiresAt':(now+pool.TOKEN_TTL).isoformat()}));return
    if a.mode=='revoke':
        sid=pool.sql_literal(s['sid']);context=pool.sql_literal(hashlib.sha256(s['csrfContext'].encode()).hexdigest())
        pool.run_mysql('START TRANSACTION; DELETE FROM csrf_tokens WHERE browser_context_hash='+context+'; DELETE FROM login_sessions WHERE user_id='+str(user)+' AND sid='+sid+'; COMMIT;')
        if pool.run_mysql('SELECT COUNT(*) FROM login_sessions WHERE user_id='+str(user)+' AND sid='+sid+';')!='0':raise ValueError('Revocation failed')
        path.unlink();print(json.dumps({'revoked':True,'remainingSessionsForSid':0,'privateFileDeleted':True}));return
    if pool.run_mysql('SELECT @@read_only,@@super_read_only;')!='0\t0':raise ValueError('Source is fenced')
    # V1__create_user_tables.sql and UserRegistrationService: default zero usage, 30GB quota.
    # Never reset an existing row or seed a real user's statistics.
    pool.run_mysql('INSERT INTO user_stats (user_id) SELECT '+str(user)+' WHERE NOT EXISTS (SELECT 1 FROM user_stats WHERE user_id='+str(user)+');')
    rows=pool.run_mysql('SELECT COUNT(*) FROM user_stats WHERE user_id='+str(user)+' AND deleted_at IS NULL;')
    if rows!='1':raise ValueError('Expected one active statistics row')
    print(json.dumps({'syntheticUserId':user,'activeStatsRows':1,'existingStatsNotReset':True}))


if __name__=='__main__':main()
