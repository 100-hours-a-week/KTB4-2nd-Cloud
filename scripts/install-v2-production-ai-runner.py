#!/usr/bin/env python3
"""SSM-only production runner install; preserve all existing shared-host services."""
import base64
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import urlparse

ROOT = Path('/opt/yeodam-production-runner')
CONFIG = Path('/etc/yeodam/production-ai')
REGION = 'ap-northeast-2'


def run(*args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError('Production runner operation failed: ' + args[0])
    return result.stdout


def aws(*args):
    result = run('aws', '--region', REGION, '--output', 'json', *args)
    return json.loads(result) if result.strip() else {}


def parameter(name):
    return aws('ssm', 'get-parameter', '--name', name, '--with-decryption')['Parameter']['Value']


def api_key():
    name = '/yeodam/v2/production/ai/API_KEY'
    result = subprocess.run(['aws', '--region', REGION, '--output', 'json', 'ssm',
                             'get-parameter', '--name', name, '--with-decryption'],
                            capture_output=True, text=True)
    if result.returncode == 0:
        value = json.loads(result.stdout)['Parameter']['Value']
    elif 'ParameterNotFound' in result.stderr:
        value = secrets.token_hex(32)
        with tempfile.NamedTemporaryFile(mode='w', dir='/run') as request:
            json.dump({'Name': name, 'Type': 'SecureString', 'Value': value}, request)
            request.flush()
            aws('ssm', 'put-parameter', '--cli-input-json', 'file://' + request.name)
    else:
        raise RuntimeError('Production API key lookup failed; no credential rotated')
    if not re.fullmatch('[0-9a-f]{64}', value):
        raise RuntimeError('Unexpected production API key format')
    return value


def host_guard():
    if os.geteuid() != 0:
        raise RuntimeError('Run on the shared GPU as root')
    token = urllib.request.urlopen(urllib.request.Request(
        'http://169.254.169.254/latest/api/token', method='PUT',
        headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'}), timeout=3).read().decode()
    request = urllib.request.Request('http://169.254.169.254/latest/meta-data/instance-id',
                                    headers={'X-aws-ec2-metadata-token': token})
    instance = urllib.request.urlopen(request, timeout=3).read().decode()
    if instance != 'i-05e1ad9dd96dba4ae':
        raise RuntimeError('Wrong shared GPU host')
    run('mountpoint', '-q', '/data/qdrant')
    device = run('findmnt', '-n', '-o', 'SOURCE', '--target', '/data/qdrant').strip()
    serial = run('lsblk', '-dn', '-o', 'SERIAL', device).strip().replace('-', '')
    if serial != 'vol05d2497eaeabb57d3':
        raise RuntimeError('Wrong Qdrant data EBS')


def install():
    os.umask(0o077)
    host_guard()
    with open('/run/yeodam-production-runner.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        image = os.environ['PRODUCTION_RUNNER_IMAGE']
        if not re.fullmatch(r'ghcr\.io/100-hours-a-week/yeodam-ai-app@sha256:[0-9a-f]{64}', image):
            raise ValueError('Immutable AI application image required')
        bucket = os.environ['PRODUCTION_PHOTO_BUCKET']
        if bucket != 'yeodam-v1-app-data-483175530259-ap-northeast-2':
            raise ValueError('Unexpected production photo bucket')
        existing = subprocess.run(['docker', 'inspect', 'yeodam-ai-process-runner-production'],
                                  capture_output=True, text=True)
        if existing.returncode == 0:
            labels = json.loads(existing.stdout)[0]['Config'].get('Labels', {})
            if labels.get('com.docker.compose.project') != 'yeodam-v2-production-ai-runner':
                raise RuntimeError('Unexpected container with production runner name')
        url = parameter('/yeodam/v2/production/ai/REDIS_URL')
        parsed = urlparse(url)
        if (parsed.scheme, parsed.hostname, parsed.port, parsed.path) != (
                'redis', 'ai-redis.production.yeodam.internal', 6379, '/0'):
            raise ValueError('Unexpected production Redis destination')
        values = {
            'API_KEY': api_key(), 'REDIS_URL': url, 'JOBS_ENV': 'production',
            'JOBS_PROCESS_HANDLER': 'app.jobs.process_handler:run',
            'JOBS_STORY_HANDLER': 'app.story.handler:run',
            'JOBS_FAKE_PIPELINE': 'false', 'FAKE_PIPELINE': 'false',
            'IMAGE_SOURCE': 's3', 'S3_BUCKET': bucket, 'AWS_REGION': REGION,
            'EMBED_MODEL': 'qwen3-vl-embedding-2b',
            'EMBEDDING_BASE_URL': 'http://ai-gpu.production.yeodam.internal:8000/v1',
            'EMBEDDING_SERVED_MODEL_NAME': 'qwen3-vl-embedding-2b',
            'EMBEDDING_API_KEY': parameter('/yeodam/v2/staging/ai/EMBEDDING_API_KEY'),
            'QDRANT_URL': 'http://ai-gpu.production.yeodam.internal:6335',
            'KAFKA_BOOTSTRAP_SERVERS': 'kafka.production.yeodam.internal:9092',
            'KAFKA_SECURITY_PROTOCOL': 'PLAINTEXT', 'RELEASE': image,
        }
        for work in ['TRIP_CREATE', 'TRIP_UPDATE', 'STORY_CREATE']:
            for direction in ['REQUEST', 'RESULT']:
                values[f'KAFKA_{work}_{direction}_TOPIC'] = f'prod.yeodam.{work.lower().replace("_", "-")}.{direction.lower()}'
        if any('\n' in value or '\r' in value for value in values.values()):
            raise ValueError('Multiline environment value rejected')
        ROOT.mkdir(mode=0o750, parents=True, exist_ok=True)
        CONFIG.mkdir(mode=0o700, parents=True, exist_ok=True)
        config = CONFIG / 'runner.env'
        config.write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
        config.chmod(0o600)
        (ROOT / 'compose.yml').write_bytes(base64.b64decode(os.environ['PRODUCTION_RUNNER_COMPOSE_B64'], validate=True))
        (ROOT / '.env').write_text('PRODUCTION_RUNNER_IMAGE=' + image + '\n')
        with tempfile.TemporaryDirectory(prefix='production-runner-', dir='/run') as directory:
            env = dict(os.environ, DOCKER_CONFIG=directory)
            auth = json.loads(aws('secretsmanager', 'get-secret-value', '--secret-id',
                                  os.environ['PRODUCTION_GHCR_SECRET'])['SecretString'])
            run('docker', 'login', 'ghcr.io', '--username', auth['username'],
                '--password-stdin', input=auth['password'], env=env)
            run('docker', 'compose', 'config', '--quiet', cwd=ROOT, env=env)
            run('docker', 'compose', 'pull', cwd=ROOT, env=env)
        # At host startup, do not claim production work before the shared model is ready.
        wait_script = '''import time,urllib.request
deadline=time.monotonic()+600
while time.monotonic()<deadline:
    try:
        for endpoint in ['http://ai-gpu.production.yeodam.internal:8000/health','http://ai-gpu.production.yeodam.internal:6335/readyz']:
            with urllib.request.urlopen(endpoint,timeout=3) as response:
                assert response.status==200
        break
    except Exception:
        time.sleep(5)
else:
    raise SystemExit('Production runner dependencies did not become ready')
'''
        wait_path = ROOT / 'wait-dependencies.py'
        wait_path.write_text(wait_script)
        unit = '''[Unit]
Description=Production process runner on shared GPU host
Requires=docker.service yeodam-production-qdrant.service
After=docker.service yeodam-production-qdrant.service network-online.target
Wants=network-online.target
RequiresMountsFor=/data/qdrant
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/yeodam-production-runner
ExecStartPre=/usr/bin/python3 /opt/yeodam-production-runner/wait-dependencies.py
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose stop
TimeoutStartSec=660
TimeoutStopSec=90
[Install]
WantedBy=multi-user.target
'''
        Path('/etc/systemd/system/yeodam-production-runner.service').write_text(unit)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', 'yeodam-production-runner')
        run('python3', str(wait_path))
        run('docker', 'compose', 'up', '-d', cwd=ROOT)
        run('systemctl', 'start', 'yeodam-production-runner')
        probe = ("import os,httpx,redis; from app.jobs.config import JobsSettings; "
                 "c=JobsSettings(); assert c.JOBS_ENV=='production' and not c.JOBS_FAKE_PIPELINE; "
                 "assert redis.Redis.from_url(os.environ['REDIS_URL'],socket_connect_timeout=5,socket_timeout=5).ping(); "
                 "httpx.get(os.environ['EMBEDDING_BASE_URL'].removesuffix('/v1')+'/health',timeout=10).raise_for_status(); "
                 "httpx.get(os.environ['QDRANT_URL']+'/readyz',timeout=10).raise_for_status()")
        for _ in range(12):
            row = json.loads(run('docker', 'inspect', 'yeodam-ai-process-runner-production'))[0]
            if row['State']['Running']:
                result = subprocess.run(['docker', 'exec', 'yeodam-ai-process-runner-production',
                                         'python', '-c', probe], capture_output=True, text=True)
                if result.returncode == 0:
                    print(json.dumps({'image': image, 'environment': 'production',
                                      'redisPing': True, 'embeddingHealth': 200,
                                      'qdrantHealth': 200, 'status': 'ready'}))
                    return
            time.sleep(5)
        raise RuntimeError('Production runner dependency readiness failed')


if __name__ == '__main__':
    try:
        install()
    except Exception as error:
        print(json.dumps({'status': 'failed', 'errorType': type(error).__name__,
                          'operation': str(error) if isinstance(error, RuntimeError) else 'configuration validation failed'}))
        raise SystemExit(1)
