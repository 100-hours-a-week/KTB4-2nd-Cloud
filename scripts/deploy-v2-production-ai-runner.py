#!/usr/bin/env python3
"""Update only the production CPU process runner; preserve secrets and shared services."""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

ROOT = Path('/opt/yeodam-production-runner')
PACKAGE = 'ghcr.io/100-hours-a-week/yeodam-ai-app'


def image_ref(value):
    if not re.fullmatch(re.escape(PACKAGE) + r'(?::sha-[0-9a-f]{40})?@sha256:[0-9a-f]{64}', value):
        raise ValueError('Unexpected runner image')
    return PACKAGE + '@' + value.rsplit('@', 1)[1]


def run(*args):
    p = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError('Runner command failed: ' + args[0])
    return p.stdout


def inspect():
    row = json.loads(run('docker', 'inspect', 'yeodam-ai-process-runner-production'))[0]
    if row['Config'].get('Labels', {}).get('com.docker.compose.project') != 'yeodam-v2-production-ai-runner':
        raise RuntimeError('Unexpected production Compose project')
    image = image_ref(row['Config']['Image'])
    # Inspect Docker's configured image as well: a stale override must not reappear on reboot.
    configured = json.loads(run('docker', 'compose', 'config', '--format', 'json'))
    configured_image = image_ref(configured['services']['process-runner']['image'])
    health = 'stopped'
    if row['State']['Running']:
        if row['Config'].get('Cmd') != ['python', '-m', 'app.jobs.runner']:
            raise RuntimeError('Unexpected runner process command')
        # This shared application image has no Docker HEALTHCHECK for the runner.
        # Probe its real Redis/model/storage dependencies without submitting a job.
        probe = ("import os,httpx,redis; assert os.environ['JOBS_ENV']=='production'; "
                 "assert redis.Redis.from_url(os.environ['REDIS_URL'],socket_connect_timeout=5,socket_timeout=5).ping(); "
                 "httpx.get(os.environ['EMBEDDING_BASE_URL'].removesuffix('/v1')+'/health',timeout=10).raise_for_status(); "
                 "httpx.get(os.environ['QDRANT_URL']+'/readyz',timeout=10).raise_for_status()")
        run('docker', 'exec', 'yeodam-ai-process-runner-production', 'python', '-c', probe)
        health = 'healthy'
    return {'image': image, 'configuredImage': configured_image, 'health': health,
            'healthCheck': 'process-running-and-dependencies'}


def main():
    if os.geteuid() != 0:
        raise RuntimeError('SSM runner deployment requires root')
    os.umask(0o077)
    if not ROOT.is_dir():
        raise RuntimeError('Production runner is not initialized')
    action = os.environ['AI_DEPLOY_ACTION']
    with open('/run/yeodam-production-runner.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if action == 'inspect':
            print(json.dumps(inspect()))
            return
        import ai_runner_runtime
        if action in {'inspect-settings', 'sync-vlm', 'restore-vlm', 'finalize-vlm'}:
            result = ai_runner_runtime.url_action('production', action,
                int(os.environ.get('AI_RUNNER_PARAMETER_VERSION', '0')), os.environ.get('AI_RUNNER_OPERATION', ''))
            print(json.dumps(result))
            return
        if action != 'deploy':
            raise ValueError('Expected inspect or deploy')
        image = image_ref(os.environ['AI_DEPLOY_IMAGE'])
        if not (ROOT / 'compose.yml').is_file() or not Path('/data/qdrant').is_mount():
            raise RuntimeError('GPU runtime is not initialized')
        inspect()
        # Credentials are short lived on this host and never reach the Actions runner.
        with tempfile.TemporaryDirectory(prefix='production-ai-deploy-', dir='/run') as directory:
            os.environ['DOCKER_CONFIG'] = directory
            secret = json.loads(run('aws', '--region', 'ap-northeast-2', 'secretsmanager',
                                    'get-secret-value', '--secret-id',
                                    'yeodam/v2/production/ghcr-credentials', '--output', 'json'))
            auth = json.loads(secret['SecretString'])
            result = subprocess.run(['docker', 'login', 'ghcr.io', '--username', auth['username'],
                                     '--password-stdin'], input=auth['password'], text=True, capture_output=True)
            if result.returncode:
                raise RuntimeError('Runner registry login failed')
            run('docker', 'pull', '--platform', 'linux/amd64', image)
            ai_runner_runtime.prepare('production', image)
            # Compose loads this durable override on normal systemd startup too.
            override = ROOT / 'compose.override.yml'
            if override.exists():
                previous = json.loads(override.read_text())
                if set(previous) != {'services'} or set(previous['services']) != {'process-runner'}:
                    raise RuntimeError('Refusing to replace an unrelated Compose override')
            content = {'services': {'process-runner': {'image': image,
                        'environment': {'RELEASE': image}, 'volumes': [dict(ai_runner_runtime.MOUNT, bind={'create_host_path': False})]}}}
            temporary = ROOT / '.runner-release.tmp'
            temporary.write_text(json.dumps(content) + '\n')
            temporary.chmod(0o600)
            temporary.replace(override)
            run('docker', 'compose', 'up', '-d', '--no-deps', 'process-runner')
        for _ in range(60):
            try:
                result = inspect()
                if result['image'] == result['configuredImage'] == image and result['health'] == 'healthy':
                    print(json.dumps(result))
                    return
            except RuntimeError:
                pass
            time.sleep(5)
        raise TimeoutError('Runner health did not become ready')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Avoid dumping Docker config, registry auth or runtime environment on error.
        print(json.dumps({'status': 'failed', 'error': str(error) if isinstance(error, (ValueError, RuntimeError, TimeoutError)) else type(error).__name__}))
        raise SystemExit(1)
