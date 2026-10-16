#!/usr/bin/env python3
"""Update a staging RunPod URL and roll only the AI API; restore on deployment failure."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess

spec = importlib.util.spec_from_file_location('ai_deploy', Path(__file__).with_name('deploy-v2-staging-ai.py'))
ai = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ai)
PARAMETER = '/yeodam/v2/staging/ai/VLM_BASE_URL'
KEY_PARAMETER = '/yeodam/v2/staging/ai/VLM_API_KEY'
PARAMETER_ARN = f'arn:aws:ssm:{ai.REGION}:{ai.ACCOUNT}:parameter{PARAMETER}'


class ProbeError(RuntimeError):
    pass


def validate_url(value):
    if not re.fullmatch(r'https://[a-z0-9]+-8000\.proxy\.runpod\.net/v1', value):
        raise ValueError('Expected https://<pod-id>-8000.proxy.runpod.net/v1 without query or fragment')
    return value


def candidate(current):
    data = json.loads(json.dumps({k: v for k, v in current.items() if k in ai.FIELDS and v is not None}))
    container = data['containerDefinitions'][0]
    container['environment'] = [e for e in container.get('environment', []) if e['name'] != 'VLM_BASE_URL']
    container['secrets'] = [e for e in container.get('secrets', []) if e['name'] != 'VLM_BASE_URL']
    container['secrets'].append({'name': 'VLM_BASE_URL', 'valueFrom': PARAMETER_ARN})
    return data


def probe(url, model):
    key = ai.aws('ssm', 'get-parameter', '--name', KEY_PARAMETER, '--with-decryption')['Parameter']['Value']
    if '\r' in key or '\n' in key:
        raise ValueError('Unexpected API key format')
    # Header through stdin: no credential in command arguments or printed response bodies.
    response = subprocess.run(['curl', '--silent', '--show-error', '--connect-timeout', '10', '--max-time', '30',
                               '--proto', '=https', '--header', '@-', '--write-out', '\n%{http_code}', url + '/models'],
                              input='Authorization: Bearer ' + key + '\n', capture_output=True, text=True)
    del key
    if response.returncode:
        raise ProbeError('RunPod connection check failed')
    body, status = response.stdout.rsplit('\n', 1)
    if status != '200':
        raise ProbeError('RunPod /models returned HTTP ' + status)
    if not any(row.get('id') == model for row in json.loads(body).get('data', [])):
        raise ProbeError('Expected VLM model is not served')
    print('RunPod model and authentication check passed', flush=True)


def promote(new_url, report, save):
    new_url = validate_url(new_url)
    if ai.aws('sts', 'get-caller-identity')['Account'] != ai.ACCOUNT:
        raise ValueError('Wrong AWS account')
    service = ai.service()
    previous = service['taskDefinition']
    if not ai.stable(service, previous):
        raise RuntimeError('AI API deployment is not stable; retry after the existing deployment')
    current = ai.definition(previous)
    environment = {e['name']: e['value'] for e in current['containerDefinitions'][0].get('environment', [])}
    old_parameter = ai.aws('ssm', 'get-parameter', '--name', PARAMETER)['Parameter']
    old_url = validate_url(old_parameter['Value'])
    report.update(previousUrl=old_url, newUrl=new_url, previousRevision=previous)
    save()
    probe(new_url, environment['VLM_MODEL'])
    target = ai.aws('elbv2', 'describe-target-groups', '--names', ai.TARGET)['TargetGroups'][0]['TargetGroupArn']
    desired = candidate(current)
    # RegisterTaskDefinition accepts JSON as a structured CLI argument; it contains only Secret ARNs.
    revision = ai.aws('ecs', 'register-task-definition', '--cli-input-json', json.dumps(desired))['taskDefinition']['taskDefinitionArn']
    report['candidateRevision'] = revision
    save()
    latest = ai.service()
    if not ai.stable(latest, previous):
        raise RuntimeError('AI API changed during preflight; no parameter was updated')
    if ai.aws('ssm', 'get-parameter', '--name', PARAMETER)['Parameter']['Version'] != old_parameter['Version']:
        raise RuntimeError('RunPod parameter changed during preflight; retry')
    report['status'] = 'updating'
    save()
    try:
        ai.aws('ssm', 'put-parameter', '--name', PARAMETER, '--type', 'String', '--value', new_url, '--overwrite')
        ai.update_api(revision, target)
    except Exception:
        report['status'] = 'recovering'
        save()
        try:
            ai.aws('ssm', 'put-parameter', '--name', PARAMETER, '--type', 'String', '--value', old_url, '--overwrite')
            ai.aws('ecs', 'update-service', '--cluster', ai.CLUSTER, '--service', ai.SERVICE,
                   '--task-definition', previous, '--force-new-deployment')
            ai.wait_api(previous, target)
            report['status'] = 'restored_previous'
        except Exception:
            report['status'] = 'recovery_required'
        save()
        raise
    report['status'] = 'healthy'
    save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', type=validate_url)
    args = parser.parse_args()
    path = Path(os.environ.get('RUNPOD_UPDATE_REPORT', 'runpod-update.json'))
    report = {'status': 'preflight'}
    def save():
        path.write_text(json.dumps(report, indent=2) + '\n')
    def interrupted(signum, frame):
        raise RuntimeError('RunPod update interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    save()
    try:
        promote(args.url, report, save)
    except Exception as error:
        if report['status'] == 'preflight':
            report['status'] = 'preflight_failed'
        report['errorType'] = type(error).__name__
        if isinstance(error, ProbeError):
            report['message'] = str(error)
            print(str(error), flush=True)
        save()
        # Do not render exceptions that could contain response bodies or credentials.
        print('RunPod update failed: ' + report['status'] + ' (' + type(error).__name__ + ')', flush=True)
        raise SystemExit(1)
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
