"""Host-only runner settings. Never return/log environment values or credentials.

Copied beside the SSM deployment scripts. Only an explicitly invoked deployment
or RunPod update calls mutating functions; importing this module has no effects.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

REGION = 'ap-northeast-2'
HOST_PATH = '/var/lib/yeodam/ai-reference'
CONTAINER_PATH = '/data/ai-reference'
INDEX = 'places-v2026-10-30.sqlite'
INDEX_URI = 's3://yeodam-v1-app-data-483175530259-ap-northeast-2/ai-reference/places/v2026-10-30/places.sqlite'
INDEX_SHA = '41f531891d9bdc5417e91c80a92e51f2e92e2661a641cb627cbdb16e5f0f3b1d'
MOUNT = {'type': 'bind', 'source': HOST_PATH, 'target': CONTAINER_PATH}
ENVIRONMENTS = {
    'staging': ('/opt/yeodam-ai', '/etc/yeodam/ai/runner.env', 'yeodam-ai-process-runner', 'yeodam-ai'),
    'production': ('/opt/yeodam-production-runner', '/etc/yeodam/production-ai/runner.env',
                   'yeodam-ai-process-runner-production', 'yeodam-v2-production-ai-runner'),
}


def run(*args, cwd=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError('Runner settings command failed: ' + args[0])
    return result.stdout


def parameter(name):
    row = json.loads(run('aws', '--region', REGION, 'ssm', 'get-parameter', '--name', name,
                         '--with-decryption', '--output', 'json'))['Parameter']
    if row.get('ARN') != f'arn:aws:ssm:{REGION}:483175530259:parameter{name}':
        raise ValueError('Runner Parameter belongs to an unexpected account or region')
    if not row['Value'] or any(c in row['Value'] for c in '\r\n\x00'):
        raise ValueError('Empty or multiline runner parameter: ' + name)
    return row


def vlm_values(environment):
    ENVIRONMENTS[environment]
    prefix = f'/yeodam/v2/{environment}/ai/'
    url = parameter(prefix + 'VLM_BASE_URL')
    if not re.fullmatch(r'https://[a-z0-9]+-8000\.proxy\.runpod\.net/v1', url['Value']):
        raise ValueError('Invalid runner VLM URL')
    return {'VLM_BASE_URL': url['Value'], 'VLM_API_KEY': parameter(prefix + 'VLM_API_KEY')['Value'],
            'VLM_MODEL': 'gemma-4'}, url['Version']


def additional_values(environment):
    values, _ = vlm_values(environment)
    values.update(PLACE_INDEX_S3_URI=INDEX_URI, PLACE_INDEX_SHA256=INDEX_SHA,
                  PLACE_INDEX_PATH=CONTAINER_PATH + '/' + INDEX,
                  KAKAO_REST_API_KEY=parameter(f'/yeodam/v2/{environment}/app/KAKAO_CLIENT_ID')['Value'])
    return values


def parse_env(text):
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or key in values:
            raise ValueError('Malformed or duplicate runner environment key')
        values[key] = value
    return values


def render_env(values):
    if any(any(c in str(v) for c in '\r\n\x00') for v in values.values()):
        raise ValueError('Multiline runner environment value')
    return ''.join(f'{key}={value}\n' for key, value in values.items())


def env_digest(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def atomic(path, content):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.runner-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def storage(image):
    # Resolve the actual image user: system-created UIDs must not be guessed.
    identity = json.loads(run('docker', 'run', '--rm', '--network', 'none', '--read-only',
                              '--entrypoint', 'python', image, '-c',
                              'import os,json;print(json.dumps([os.geteuid(),os.getegid()]))'))
    if len(identity) != 2 or not all(type(i) is int and i > 0 for i in identity):
        raise ValueError('Runner image must have a non-root UID/GID')
    path = Path(HOST_PATH)
    if path.is_symlink():
        raise ValueError('Reference directory must not be a symlink')
    path.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(path).free < 2_200_000_000:
        raise RuntimeError('Reference filesystem needs at least 2.2 GB free')
    if any(path.iterdir()) and (path.stat().st_uid, path.stat().st_gid) != tuple(identity):
        raise RuntimeError('Existing shared reference data has another owner; inspect before changing it')
    os.chown(path, *identity)
    path.chmod(0o750)


def prepare(environment, image):
    _, env_path, _, _ = ENVIRONMENTS[environment]
    path = Path(env_path)
    values = parse_env(path.read_text())
    if values.get('JOBS_ENV') != environment:
        raise ValueError('Runner environment does not match the target')
    additions = additional_values(environment)  # Read everything before writing.
    storage(image)
    values.update(additions)
    atomic(path, render_env(values))


def container(environment):
    _, _, name, project = ENVIRONMENTS[environment]
    row = json.loads(run('docker', 'inspect', name))[0]
    if (row['Config'].get('Labels', {}).get('com.docker.compose.project') != project
            or row['Config'].get('Cmd') != ['python', '-m', 'app.jobs.runner']):
        raise ValueError('Unexpected runner container or Compose project')
    return row


def runner_ready(environment, expected):
    _, _, name, _ = ENVIRONMENTS[environment]
    first = None
    for _ in range(90):
        row = container(environment)
        actual = parse_env('\n'.join(row['Config'].get('Env', [])))
        marker = (row['Id'], row['RestartCount'])
        if row['State']['Running'] and all(actual.get(k) == v for k, v in expected.items()):
            if first == marker:
                # Authenticated model-list probe, no generated tokens/jobs.
                probe = ("import os,httpx; r=httpx.get(os.environ['VLM_BASE_URL']+'/models',"
                         "headers={'Authorization':'Bearer '+os.environ['VLM_API_KEY']},timeout=15);"
                         "r.raise_for_status(); assert any(x.get('id')==os.environ['VLM_MODEL'] "
                         "for x in r.json().get('data',[]))")
                run('docker', 'exec', name, 'python', '-c', probe)
                return
            first = marker
        else:
            first = None
        time.sleep(5)
    raise TimeoutError('Runner VLM settings did not become stable')


def url_action(environment, action, version=0, operation=''):
    root, env_path, _, _ = ENVIRONMENTS[environment]
    path = Path(env_path)
    values = parse_env(path.read_text())
    if values.get('JOBS_ENV') != environment:
        raise ValueError('Wrong runner environment')
    row = container(environment)
    actual = parse_env('\n'.join(row['Config'].get('Env', [])))
    if action == 'inspect-settings':
        expected, _ = vlm_values(environment)
        mounts = [m for m in row.get('Mounts', []) if m.get('Destination') == CONTAINER_PATH]
        if len(mounts) != 1 or mounts[0].get('Source') != HOST_PATH or mounts[0].get('RW') is not True:
            raise RuntimeError('Runner reference mount is missing or read-only; deploy its configuration first')
        if not row['State']['Running'] or not all(values.get(k) == actual.get(k) == v for k, v in expected.items()):
            raise RuntimeError('Runner VLM settings are not aligned; prepare/recreate it before updating RunPod')
        return {'settingsReady': True}
    if not re.fullmatch(r'[0-9a-f]{32}', operation):
        raise ValueError('A unique RunPod operation ID is required')
    directory = Path('/run/yeodam-runner-settings') / environment
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = directory / (operation + '.json')
    if action == 'sync-vlm':
        if not row['State']['Running'] or any(values.get(k) != actual.get(k) for k in ['VLM_BASE_URL', 'VLM_API_KEY', 'VLM_MODEL']):
            raise RuntimeError('Runner changed after preflight; inspect before updating it')
        expected, current_version = vlm_values(environment)
        if version <= 0 or current_version != version:
            raise RuntimeError('RunPod Parameter version changed before runner update')
        if backup.exists():
            raise RuntimeError('Operation already exists; inspect before retrying')
        old = path.read_text()
        values.update(expected)
        new = render_env(values)
        atomic(backup, json.dumps({'old': old, 'newSha256': hashlib.sha256(new.encode()).hexdigest(),
                                  'imageId': row['Image'], 'runtimeOld': env_digest(actual),
                                  'runtimeNew': env_digest(dict(actual, **expected))}))
        atomic(path, new)
        run('docker', 'compose', 'up', '-d', '--no-deps', '--force-recreate', 'process-runner', cwd=root)
        runner_ready(environment, expected)
        return {'settingsReady': True, 'parameterVersion': current_version}
    if action == 'restore-vlm':
        data = json.loads(backup.read_text())
        allowed_files = {data['newSha256'], hashlib.sha256(data['old'].encode()).hexdigest()}
        if (row['Image'] != data['imageId'] or hashlib.sha256(path.read_bytes()).hexdigest() not in allowed_files
                or env_digest(actual) not in {data['runtimeOld'], data['runtimeNew']}):
            raise RuntimeError('Runner changed externally; refusing to overwrite it')
        old = parse_env(data['old'])
        expected, current_version = vlm_values(environment)
        if current_version != version or not all(old.get(k) == v for k, v in expected.items()):
            raise RuntimeError('Restored Parameter does not match the runner backup')
        atomic(path, data['old'])
        run('docker', 'compose', 'up', '-d', '--no-deps', '--force-recreate', 'process-runner', cwd=root)
        runner_ready(environment, expected)
        backup.unlink()
        return {'settingsReady': True}
    if action == 'finalize-vlm':
        backup.unlink(missing_ok=True)
        return {'finalized': True}
    raise ValueError('Unsupported runner settings action')
