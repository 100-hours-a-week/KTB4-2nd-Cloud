#!/usr/bin/env python3
"""Stage the V1 edge gate without installing packages/reloading. Activation is explicit."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

APP = 'i-05d320e3a87200d5a'
SITE = Path('/etc/nginx/sites-available/yeodam-v1-staging')
MAIN = Path('/etc/nginx/nginx.conf')
LIVE = Path('/etc/yeodam/nginx-jwt-gate')
STAGE = Path('/var/lib/yeodam/jwt-gate468')
DIGEST = 'sha256:3f1cb707caa276a7b834d92279d3738184f0b7c8b681e876544f0cc9612dc2b8'
MODULE = Path('/usr/lib/nginx/modules/ngx_http_js_module.so')
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from nginx_jwt_gate import instrument_jwt_gate


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def write(path, data, mode):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as f:
        f.write(data); f.flush(); os.fsync(f.fileno())

def replace(path, data, mode):
    temp = path.with_name(path.name + '.468-tmp')
    write(temp, data, mode)
    os.replace(temp, path)

def run(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=35)
    if result.returncode:
        # Never include docker environment or full command output in errors.
        raise RuntimeError('Command failed: ' + Path(args[0]).name)
    return result.stdout

def backend():
    ids = run(['docker','ps','--filter','label=com.docker.compose.service=backend','-q']).split()
    if len(ids) != 1: raise ValueError('Expected one V1 backend')
    c = json.loads(run(['docker','inspect',ids[0]]))[0]
    image = c['Config']['Image']
    if not image.endswith('@'+DIGEST) or c['State'].get('Health',{}).get('Status') != 'healthy':
        raise ValueError('Reviewed healthy V1 backend required')
    env = dict(x.split('=',1) for x in c['Config'].get('Env',[]) if '=' in x)
    required = {'V1_USER_READ_RATE_LIMIT_ENABLED':'true','V1_USER_READ_RATE_LIMIT_RPS':'5',
                'V1_USER_READ_RATE_LIMIT_BURST':'30','V1_USER_READ_RATE_LIMIT_MAX_USERS':'10000'}
    if any(env.get(k) != v for k,v in required.items()): raise ValueError('Expected active BE user limiter')
    key = env.get('JWT_SECRET',''); issuer = env.get('JWT_ISSUER','')
    if len(key.encode()) < 32 or not issuer: raise ValueError('JWT configuration missing')
    # Audience is fixed in the pinned V1 application.yaml; honor an explicit override.
    audience = env.get('AUTH_JWT_AUDIENCE','yeodam-api')
    if audience != 'yeodam-api': raise ValueError('Unexpected V1 audience override')
    return {'secret':key,'issuer':issuer,'audience':audience,'clockSkewSeconds':60}

def assert_host():
    if os.geteuid() != 0 or Path('/var/lib/cloud/data/instance-id').read_text().strip() != APP:
        raise ValueError('Only the original V1 staging App root session is allowed')
    if SITE.is_symlink() or not SITE.is_file(): raise ValueError('Expected regular staging site file')

def metadata():
    if not STAGE.is_dir() or STAGE.is_symlink(): raise ValueError('Stage missing')
    return json.loads((STAGE/'receipt.json').read_text())

def stage(args):
    if sha(SITE) != args.expected_site_sha or sha(MAIN) != args.expected_main_sha:
        raise ValueError('Running configuration drift; inspect before staging')
    config = backend()
    original = SITE.read_bytes()
    candidate = instrument_jwt_gate(original.decode(), DIGEST).encode()
    STAGE.mkdir(mode=0o700) # Refuse to overwrite existing prepared state.
    write(STAGE/'site-before.conf', original, 0o600)
    write(STAGE/'site-candidate.conf', candidate, 0o600)
    write(STAGE/'config.json', (json.dumps(config)+'\n').encode(), 0o600)
    write(STAGE/'gate.mjs', (Path(__file__).parent/'gate.mjs').read_bytes(), 0o600)
    receipt = {'instance':APP,'preparedAt':datetime.now(timezone.utc).isoformat(),
               'beforeSha':sha(SITE),'mainSha':sha(MAIN),'candidateSha':sha(STAGE/'site-candidate.conf'),
               'gateSha':sha(STAGE/'gate.mjs'),'backendDigest':DIGEST,
               'mode':'staged-only; package/install/reload/public requests not executed'}
    write(STAGE/'receipt.json', (json.dumps(receipt,indent=2)+'\n').encode(), 0o600)
    if sha(SITE) != receipt['beforeSha'] or sha(MAIN) != receipt['mainSha']:
        raise ValueError('Active files changed concurrently')
    return receipt

def activate(args):
    if args.confirm_instance != APP: raise ValueError('Explicit instance confirmation required')
    m = metadata()
    if sha(SITE) != m['beforeSha'] or sha(MAIN) != m['mainSha']:
        raise ValueError('Configuration drift; activation refused')
    if not MODULE.is_file(): raise ValueError('Install the matching njs module in the approved maintenance window first')
    if sha(STAGE/'site-candidate.conf') != m['candidateSha'] or sha(STAGE/'gate.mjs') != m['gateSha']:
        raise ValueError('Prepared files changed')
    config = backend()
    if config != json.loads((STAGE/'config.json').read_text()): raise ValueError('JWT runtime changed; restage after inspection')
    if LIVE.exists(): raise ValueError('Live JWT path exists; inspect rather than overwrite')
    LIVE.mkdir(mode=0o755)
    write(LIVE/'config.json', (STAGE/'config.json').read_bytes(), 0o600)
    write(LIVE/'gate.mjs', (STAGE/'gate.mjs').read_bytes(), 0o644)
    try:
        replace(SITE, (STAGE/'site-candidate.conf').read_bytes(), 0o644)
        run(['nginx','-t'])
        run(['systemctl','reload','nginx'])
    except Exception:
        replace(SITE, (STAGE/'site-before.conf').read_bytes(), 0o644)
        run(['nginx','-t']);run(['systemctl','reload','nginx'])
        shutil.rmtree(LIVE)
        raise
    return {'instance':APP,'state':'activated; live/load validation still required','siteSha':sha(SITE)}

def rollback(args):
    if args.confirm_instance != APP: raise ValueError('Explicit instance confirmation required')
    m = metadata()
    if sha(SITE) != m['candidateSha'] or sha(MAIN) != m['mainSha']:
        raise ValueError('Unexpected active files; rollback refused')
    replace(SITE, (STAGE/'site-before.conf').read_bytes(), 0o644)
    run(['nginx','-t']);run(['systemctl','reload','nginx'])
    # The idle module may stay installed. It is no longer referenced by this site.
    if LIVE.exists(): shutil.rmtree(LIVE)
    return {'instance':APP,'state':'restored original user5/IP25 profile','siteSha':sha(SITE)}

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    s=sub.add_parser('stage');s.add_argument('--expected-site-sha',required=True);s.add_argument('--expected-main-sha',required=True)
    for a in ['activate','rollback']:
        sub.add_parser(a).add_argument('--confirm-instance',required=True)
    sub.add_parser('status')
    args=p.parse_args();assert_host()
    if args.action == 'stage': out=stage(args)
    elif args.action == 'activate': out=activate(args)
    elif args.action == 'rollback': out=rollback(args)
    else:
        out={'instance':APP,'siteSha':sha(SITE),'mainSha':sha(MAIN),'staged':STAGE.is_dir(),'modulePresent':MODULE.is_file()}
        if STAGE.is_dir():out['prepared']=metadata()
    print(json.dumps(out,ensure_ascii=False))

if __name__=='__main__':
    try: main()
    except Exception as exc:
        # Only fixed error messages. Do not print arbitrary subprocess/configuration details.
        print(json.dumps({'error':type(exc).__name__,'message':str(exc) if isinstance(exc,(ValueError,RuntimeError)) else 'Preparation/activation failed'}),file=sys.stderr)
        sys.exit(1)
