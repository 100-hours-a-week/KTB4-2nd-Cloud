#!/usr/bin/env python3
"""Docker-only AOF/TIME regression using the pinned BE or AI Redis runtime.

No AWS, cluster, published ports, external network or real credentials. For BE Redis, the sole
Sentinel substitution is wait_primary() -> redis-0, since this tests AOF/ACL,
not quorum discovery. All other start script, ACL, TLS and persistence settings
are copied from the actual Chart. Temporary data and containers are removed.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'deploy/v3/redis/files'


def docker(*args, check=True, timeout=60):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('Docker ' + str(args[0]) + ' failed (exit ' + str(result.returncode) + ')')
    return result


def exercise(image, vulnerable, store="app"):
    name = 'v3-' + store + '-redis-aof-' + secrets.token_hex(6)
    runtime_source = ROOT / 'deploy/v3/ai-storage/files/redis' if store == 'ai' else RUNTIME
    hostname = 'ai-redis.ai-redis.svc.cluster.local' if store == 'ai' else 'redis-0.redis-members.redis.svc.cluster.local'
    with tempfile.TemporaryDirectory(prefix=name) as directory:
        root = Path(directory)
        root.chmod(0o755)
        runtime, credentials, data = (root / x for x in ('runtime', 'credentials', 'data'))
        for path in (runtime, credentials, data):
            path.mkdir(mode=0o755)
        data.chmod(0o777)  # Scratch only; Redis itself runs as the Chart's UID999.
        if store == 'ai':
            spec = importlib.util.spec_from_file_location('ai_bootstrap', ROOT / 'scripts/prepare-v3-ai-storage-secrets.py')
            bootstrap = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bootstrap)
            with tempfile.TemporaryDirectory(prefix='v3-ai-aof-fixture-') as private:
                generated = Path(private) / 'generated'
                bootstrap.prepare(generated)
                payload = json.loads((generated / 'ai-redis/source.json').read_text())
            mapping = {'admin': 'adminPassword', 'app': 'appPassword', 'health': 'healthPassword'}
        else:
            spec = importlib.util.spec_from_file_location('bootstrap', ROOT / 'scripts/create-v3-redis-bootstrap-secret.py')
            bootstrap = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bootstrap)
            with tempfile.TemporaryDirectory(prefix='v3-aof-fixture-') as private:
                payload = bootstrap.create_payload(private)
            mapping = {'admin': 'adminPassword', 'app': 'appPassword', 'replication': 'replicationPassword',
                       'monitor': 'monitorPassword', 'health': 'healthPassword'}
        for user, key in mapping.items():
            (credentials / (user + '-password')).write_text(payload[key])
        for name_, key in [('ca.crt', 'tlsCa'), ('tls.crt', 'tlsCert'), ('tls.key', 'tlsKey')]:
            (credentials / name_).write_text(payload[key])
        for path in credentials.iterdir():
            path.chmod(0o644)  # Disposable fixture, never production credentials.
        for path in runtime_source.iterdir():
            (runtime / path.name).write_text(path.read_text())
        if store == 'app':
            common = runtime / 'common.sh'
            common.write_text(common.read_text() + '\nwait_primary() { printf %s redis-0.redis-members.redis.svc.cluster.local; }\n')
        start = runtime / ('start.sh' if store == 'ai' else 'start-redis.sh')
        if vulnerable:
            old = 'user default reset off ~* &* +@all'
            if old not in start.read_text():
                raise RuntimeError('Reviewed fixed ACL not found')
            start.write_text(start.read_text().replace(old, 'user default off'))
        for path in runtime.iterdir():
            path.chmod(0o644)

        def cli(command, user='app', auth=True, check=True):
            # No credentials in process arguments; all values are fixture files.
            prefix = 'export REDISCLI_AUTH="$(cat /credentials/' + user + '-password)"; ' if auth else ''
            argv = ['exec', name, '/bin/bash', '-ec', prefix +
                    'exec redis-cli --no-auth-warning --tls --cacert /credentials/ca.crt '
                    '-h 127.0.0.1 --sni ' + hostname + ' '
                    + ('--user ' + user + ' ' if auth else '') + '--raw -e ' + command]
            return docker(*argv, check=check)

        def check_app_clock():
            clock = cli('TIME').stdout.splitlines()
            if len(clock) != 2 or not all(value.isdigit() for value in clock):
                raise RuntimeError('App TIME access failed')

        def ready():
            for _ in range(80):
                if cli('PING', check=False).stdout.strip() == 'PONG':
                    return True
                if docker('inspect', '-f', '{{.State.Running}}', name).stdout.strip() != 'true':
                    return False
                time.sleep(0.25)
            return False

        try:
            docker('run', '-d', '--name', name, '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                   '--security-opt', 'no-new-privileges', '--user', '999:999', '--memory', '256m', '--cpus', '0.25',
                   '--tmpfs', '/tmp:rw,nosuid,nodev,size=8m,uid=999,gid=999',
                   '-v', str(runtime) + ':/runtime:ro', '-v', str(credentials) + ':/credentials:ro',
                   '-v', str(data) + ':/data', '-e', 'POD_NAME=redis-0', '-e', 'INITIAL_BOOTSTRAP=true',
                   image, '/bin/sh' if store == 'ai' else '/bin/bash', '/runtime/' + start.name)
            if not ready():
                raise RuntimeError('Initial Redis startup failed')
            if cli('GET v3-aof:control', auth=False, check=False).returncode == 0:
                raise RuntimeError('Unauthenticated access accepted')
            denied = docker('exec', name, 'redis-cli', '--tls', '--cacert', '/credentials/ca.crt',
                            '-h', '127.0.0.1', '--user', 'default', '-a', 'fixture-invalid', '-e', 'PING', check=False)
            if denied.returncode == 0:
                raise RuntimeError('Default user login accepted')
            if cli('CONFIG GET maxmemory', check=False).returncode == 0:
                raise RuntimeError('App CONFIG access accepted')
            check_app_clock()
            # Reproduce missing TIME rights with fixture-only credentials/data.
            cli('ACL SETUSER app -time', user='admin')
            denied_time = cli('TIME', check=False)
            if denied_time.returncode == 0 or 'NOPERM' not in denied_time.stdout + denied_time.stderr:
                raise RuntimeError('Old app ACL TIME denial was not reproduced')
            cli('ACL SETUSER app +time', user='admin')
            check_app_clock()
            cli('SET v3-aof:control plain')
            transaction = 'MULTI\nHSET v3-aof:state status queued\nZADD v3-aof:queue 1 item1\nEXEC\n'
            result = subprocess.run(['docker', 'exec', '-i', name, '/bin/bash', '-ec',
                'export REDISCLI_AUTH="$(cat /credentials/app-password)"; '
                'exec redis-cli --no-auth-warning --tls --cacert /credentials/ca.crt -h 127.0.0.1 --user app --raw -e'],
                input=transaction, capture_output=True, text=True, timeout=30)
            if result.returncode or result.stdout.splitlines() != ['OK', 'QUEUED', 'QUEUED', '1', '1']:
                raise RuntimeError('Transaction write failed')
            if cli('HGET v3-aof:state status').stdout.strip() != 'queued':
                raise RuntimeError('Pre-restart transaction missing')
            docker('stop', '-t', '20', name)
            docker('start', name)
            started = ready()
            logs = docker('logs', name).stdout + docker('logs', name).stderr
            replay_denied = 'NOPERM' in logs
            restored = started and cli('HGET v3-aof:state status').stdout.strip() == 'queued' and \
                cli('ZSCORE v3-aof:queue item1').stdout.strip() == '1'
            if vulnerable:
                if not replay_denied or restored:
                    raise RuntimeError('Vulnerable AOF condition was not reproduced')
            else:
                if not restored or replay_denied or cli('GET v3-aof:control').stdout.strip() != 'plain':
                    raise RuntimeError('Fixed AOF data restoration failed')
                if cli('CONFIG GET maxmemory', check=False).returncode == 0:
                    raise RuntimeError('Post-restart app admin access accepted')
                check_app_clock()
                cli('DEL v3-aof:control v3-aof:state v3-aof:queue')
                if cli('EXISTS v3-aof:control v3-aof:state v3-aof:queue').stdout.strip() != '0':
                    raise RuntimeError('Fixture cleanup failed')
            oom = docker('inspect', '-f', '{{.State.OOMKilled}}', name).stdout.strip()
            if oom != 'false':
                raise RuntimeError('Redis fixture exceeded memory limit')
            return {'store': store, 'mode': 'old-acl' if vulnerable else 'fixed-acl', 'aofReplayDenied': replay_denied,
                    'transactionRestored': bool(restored), 'oldAclTimeDenied': True,
                    'oauthTimeAfterRestart': True if not vulnerable and store == 'app' else None,
                    'appTimeAfterRestart': True if not vulnerable else None, 'oomKilled': False}
        finally:
            docker('rm', '-f', name)
            # UID999 owns the bind-mounted AOF dirs; make only this scratch tree
            # removable by the host runner before TemporaryDirectory cleanup.
            docker('run', '--rm', '--network', 'none', '--user', '999:999', '--cap-drop', 'ALL',
                   '-v', str(data) + ':/data', image, '/bin/bash', '-ec', 'find /data -mindepth 1 -maxdepth 1 -exec chmod -R a+rwX {} +')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', choices=['app','ai'], default='app')
    parser.add_argument('--image', help='CI-only same-digest Redis mirror override')
    args = parser.parse_args()
    store = args.store
    if store == 'ai':
        match = re.search(r'^  redis: (public\.ecr\.aws/docker/library/redis@sha256:[a-f0-9]{64})$',
                          (ROOT / 'deploy/v3/ai-storage/values.yaml').read_text(), re.M)
    else:
        match = re.search(r'^image: (public\.ecr\.aws/docker/library/redis@sha256:[a-f0-9]{64})$',
                          (ROOT / 'deploy/v3/redis/values.yaml').read_text(), re.M)
    if not match:
        raise RuntimeError('Pinned Redis digest required')
    image = match[1]
    if args.image:
        from ci_image_transport import REF
        original = REF.fullmatch(image)
        replacement = REF.fullmatch(args.image)
        if not original or not replacement or replacement[2] != original[2]:
            raise ValueError('CI mirror must preserve the configured immutable Digest')
        image = args.image
    docker('image', 'inspect', image)
    results = [exercise(image, False, store)] if store == 'ai' else [exercise(image, True), exercise(image, False)]
    print(json.dumps({'results': results, 'network': 'none', 'memoryLimitMi': 256,
                      'awsAccessed': False, 'sentinelIntegrationTested': False, 'temporaryResourcesRemoved': True}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit('Isolated Redis AOF check failed (' + type(error).__name__ + '): ' + str(error))
