"""Fail before runtime if the CI stack loses isolation or uses non-V2 images."""
import ast
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]


def main():
    with tempfile.TemporaryDirectory() as directory:
        env = os.environ | {
            'V2_FRONTEND_IMAGE': 'ghcr.io/100-hours-a-week/yeodam-frontend@sha256:' + '0' * 64,
            'V2_BACKEND_IMAGE': 'ghcr.io/100-hours-a-week/yeodam-backend@sha256:' + '1' * 64,
            'V2_AI_APP_IMAGE': 'ghcr.io/100-hours-a-week/yeodam-ai-app@sha256:' + '2' * 64,
            'V2_CI_RUNTIME': directory, 'V2_CI_RESULTS_DIRECTORY': directory,
        }
        result = subprocess.run(['docker', 'compose', '--profile', 'tools', '-f',
                                 'tests/ci/v2/compose.yml', 'config', '--format', 'json'],
                                cwd=ROOT, env=env, capture_output=True, text=True, check=True)
        config = json.loads(result.stdout)
    assert config['networks']['default']['internal'] is True
    for service in config['services'].values():
        assert not service.get('ports') and not service.get('network_mode')
        assert not service.get('privileged') and not service.get('devices')
        assert not service.get('build'), 'Gate must use published images'
        assert set(service['networks']) == {'default'}
        for volume in service.get('volumes', []):
            assert '.aws' not in volume.get('source', '') and 'docker.sock' not in volume.get('source', '')
        variables = service.get('environment', {})
        assert 'AWS_PROFILE' not in variables and 'AWS_SESSION_TOKEN' not in variables
        if 'AWS_ACCESS_KEY_ID' in variables:
            assert variables['AWS_ACCESS_KEY_ID'] == variables['AWS_SECRET_ACCESS_KEY'] == 'ci-only'
    services = config['services']
    for service, variable in [('frontend', 'V2_FRONTEND_IMAGE'), ('backend', 'V2_BACKEND_IMAGE'),
                              ('ai-api', 'V2_AI_APP_IMAGE'), ('process-runner', 'V2_AI_APP_IMAGE')]:
        assert services[service]['image'] == env[variable]
        assert services[service]['platform'] == 'linux/amd64'
    for name in ['ai-api', 'process-runner']:
        variables = services[name]['environment']
        assert variables['JOBS_FAKE_PIPELINE'] == variables['FAKE_PIPELINE'] == 'false'
        assert variables['JOBS_PROCESS_HANDLER'] == 'app.jobs.process_handler:run'
        assert variables['EMBEDDING_BASE_URL'] == variables['VLM_BASE_URL'] == 'http://models:8000/v1'
    assert services['backend']['environment']['SPRING_PROFILES_ACTIVE'] == 'prod'
    assert services['backend']['environment']['APP_TRIP_RESULT_LISTENER_ENABLED'] == 'true'
    assert services['backend']['environment']['ATTACHMENT_S3_ENDPOINT'] == 'http://s3:4566'
    for path in Path(__file__).parent.glob('*.py'):
        ast.parse(path.read_text())
    print('V2 CI image selection, syntax and isolation validated')


if __name__ == '__main__':
    main()
