#!/usr/bin/env python3
"""Update one environment's RunPod URL and roll its AI API with local recovery."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

spec = importlib.util.spec_from_file_location('ai_deploy', Path(__file__).with_name('deploy-v2-staging-ai.py'))
ai = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ai)
ENVIRONMENTS = {
    'staging': ('yeodam-v2-staging-app', 'yeodam-v2-staging-ai-api', 'yeodam-v2-stg-ai-api'),
    'production': ('yeodam-v2-production-app', 'yeodam-v2-production-ai-api', 'yeodam-v2-prod-ai-api'),
}
STAGING_PARAMETER = '/yeodam/v2/staging/ai/VLM_BASE_URL'


class ProbeError(RuntimeError):
    pass


def validate_url(value):
    if not re.fullmatch(r'https://[a-z0-9]+-8000\.proxy\.runpod\.net/v1', value):
        raise ValueError('Expected https://<pod-id>-8000.proxy.runpod.net/v1 without query or fragment')
    return value


def candidate(environment, current):
    data = json.loads(json.dumps({k: v for k, v in current.items() if k in ai.FIELDS and v is not None}))
    container = data['containerDefinitions'][0]
    container['environment'] = [e for e in container.get('environment', []) if e['name'] != 'VLM_BASE_URL']
    container['secrets'] = [e for e in container.get('secrets', []) if e['name'] != 'VLM_BASE_URL']
    container['secrets'].append({'name': 'VLM_BASE_URL', 'valueFrom': f'arn:aws:ssm:{ai.REGION}:{ai.ACCOUNT}:parameter{parameter(environment)}'})
    return data


def probe(environment, url, model):
    key = ai.aws('ssm', 'get-parameter', '--name', parameter(environment, 'VLM_API_KEY'), '--with-decryption')['Parameter']['Value']
    if '\r' in key or '\n' in key or not key:
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


def parameter(environment, name='VLM_BASE_URL'):
    if environment not in ENVIRONMENTS:
        raise ValueError('Unexpected environment')
    return f'/yeodam/v2/{environment}/ai/{name}'


def service(environment):
    cluster, name, _ = ENVIRONMENTS[environment]
    data = ai.aws('ecs', 'describe-services', '--cluster', cluster, '--services', name)
    rows = data.get('services', [])
    if data.get('failures') or len(rows) != 1 or rows[0]['serviceName'] != name:
        raise RuntimeError('Expected AI API service not found')
    return rows[0]


def definition(environment, arn):
    row = ai.aws('ecs', 'describe-task-definition', '--task-definition', arn)['taskDefinition']
    if (row['family'] != ENVIRONMENTS[environment][1] or row['status'] != 'ACTIVE'
            or [c['name'] for c in row['containerDefinitions']] != ['ai-api']):
        raise ValueError('Unexpected AI API task definition')
    ai.image_ref(row['containerDefinitions'][0]['image'])
    container = row['containerDefinitions'][0]
    expected_key = f'arn:aws:ssm:{ai.REGION}:{ai.ACCOUNT}:parameter{parameter(environment, "VLM_API_KEY")}'
    keys = [v['valueFrom'] for v in container.get('secrets', []) if v['name'] == 'VLM_API_KEY']
    if keys != [expected_key] or any(v['name'] == 'VLM_API_KEY' for v in container.get('environment', [])):
        raise ValueError('AI API must use its own environment VLM API key')
    return row


def target_group(environment, row):
    groups = row.get('loadBalancers', [])
    prefix = f'arn:aws:elasticloadbalancing:{ai.REGION}:{ai.ACCOUNT}:targetgroup/{ENVIRONMENTS[environment][2]}/'
    if len(groups) != 1 or not groups[0]['targetGroupArn'].startswith(prefix):
        raise ValueError('Unexpected AI API target group')
    return groups[0]['targetGroupArn']


def healthy_tasks(environment, row, target):
    cluster, name, _ = ENVIRONMENTS[environment]
    ids = ai.aws('ecs', 'list-tasks', '--cluster', cluster, '--service-name', name)['taskArns']
    if not ids:
        return False
    tasks = ai.aws('ecs', 'describe-tasks', '--cluster', cluster, '--tasks', *ids)
    if tasks.get('failures'):
        return False
    current = [t for t in tasks['tasks'] if t['taskDefinitionArn'] == row['taskDefinition']
               and t['lastStatus'] == 'RUNNING' and t.get('healthStatus') == 'HEALTHY']
    ips = {n['privateIpv4Address'] for t in current for c in t['containers']
           for n in c.get('networkInterfaces', []) if n.get('privateIpv4Address')}
    health = ai.aws('elbv2', 'describe-target-health', '--target-group-arn', target)['TargetHealthDescriptions']
    ready = {t['Target']['Id'] for t in health if t['TargetHealth']['State'] == 'healthy'}
    return len(current) >= row['desiredCount'] and len(ips) >= row['desiredCount'] and ips.issubset(ready)


def wait_api(environment, expected, target):
    for _ in range(90):
        row = service(environment)
        if target_group(environment, row) != target:
            raise RuntimeError('AI API target group changed during deployment')
        if ai.stable(row, expected) and healthy_tasks(environment, row, target):
            return
        if any(d.get('rolloutState') == 'FAILED' and d['taskDefinition'] == expected
               for d in row.get('deployments', [])):
            raise RuntimeError('AI API deployment circuit breaker failed')
        time.sleep(10)
    raise TimeoutError('AI ECS rollout or current Task/ALB health timed out')


def attest_staging(new_url, expected_version):
    if not isinstance(expected_version, int) or expected_version <= 0:
        raise ValueError('Production requires the successful staging Parameter version')
    row = ai.aws('ssm', 'get-parameter', '--name', STAGING_PARAMETER)['Parameter']
    if row['Value'] != new_url or row['Version'] != expected_version:
        raise RuntimeError('Staging URL changed after this run; start a new update workflow')


def update(environment, revision, force=False):
    cluster, name, _ = ENVIRONMENTS[environment]
    args = ['ecs', 'update-service', '--cluster', cluster, '--service', name, '--task-definition', revision]
    if force:
        args.append('--force-new-deployment')
    ai.aws(*args)


def promote(environment, new_url, report, save, staging_version=None):
    new_url = validate_url(new_url)
    parameter(environment)
    if ai.aws('sts', 'get-caller-identity')['Account'] != ai.ACCOUNT:
        raise ValueError('Wrong AWS account')
    if environment == 'production':
        attest_staging(new_url, staging_version)
    current_service = service(environment)
    previous = current_service['taskDefinition']
    if not ai.stable(current_service, previous):
        raise RuntimeError('AI API deployment is not stable; retry after the existing deployment')
    target = target_group(environment, current_service)
    if not healthy_tasks(environment, current_service, target):
        raise RuntimeError('Current AI Task/ALB health is incomplete')
    current = definition(environment, previous)
    settings = {e['name']: e['value'] for e in current['containerDefinitions'][0].get('environment', [])}
    old_parameter = ai.aws('ssm', 'get-parameter', '--name', parameter(environment))['Parameter']
    old_url = validate_url(old_parameter['Value'])
    report.update(environment=environment, previousUrl=old_url, newUrl=new_url, previousRevision=previous,
                  expectedStagingVersion=staging_version)
    save()
    probe(environment, new_url, settings['VLM_MODEL'])
    desired = candidate(environment, current)
    # RegisterTaskDefinition accepts JSON as a structured CLI argument; it contains only Secret ARNs.
    revision = ai.aws('ecs', 'register-task-definition', '--cli-input-json', json.dumps(desired))['taskDefinition']['taskDefinitionArn']
    report['candidateRevision'] = revision
    save()
    latest = service(environment)
    if not ai.stable(latest, previous) or target_group(environment, latest) != target:
        raise RuntimeError('AI API changed during preflight; no parameter was updated')
    if ai.aws('ssm', 'get-parameter', '--name', parameter(environment))['Parameter']['Version'] != old_parameter['Version']:
        raise RuntimeError('RunPod parameter changed during preflight; retry')
    if environment == 'production':
        attest_staging(new_url, staging_version)
    report['status'] = 'updating'
    save()
    try:
        written = ai.aws('ssm', 'put-parameter', '--name', parameter(environment), '--type', 'String', '--value', new_url, '--overwrite')
        report['parameterVersion'] = written['Version']
        save()
        update(environment, revision)
        wait_api(environment, revision, target)
        probe(environment, new_url, settings['VLM_MODEL'])
    except Exception:
        report['status'] = 'recovering'
        save()
        try:
            if 'parameterVersion' not in report:
                raise RuntimeError('Parameter write result is unknown; inspect before recovery')
            recovery_service = service(environment)
            if (recovery_service['taskDefinition'] not in {previous, revision}
                    or target_group(environment, recovery_service) != target):
                raise RuntimeError('AI service changed externally; refusing to overwrite it during recovery')
            latest_parameter = ai.aws('ssm', 'get-parameter', '--name', parameter(environment))['Parameter']
            if latest_parameter['Value'] != new_url or latest_parameter['Version'] != report['parameterVersion']:
                raise RuntimeError('Parameter changed externally; refusing to overwrite it during recovery')
            ai.aws('ssm', 'put-parameter', '--name', parameter(environment), '--type', 'String', '--value', old_url, '--overwrite')
            update(environment, previous, force=True)
            wait_api(environment, previous, target)
            report['status'] = 'restored_previous'
        except Exception:
            report['status'] = 'recovery_required'
        save()
        raise
    report['status'] = 'healthy'
    save()


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('url', type=validate_url)
    parser.add_argument('--environment', choices=ENVIRONMENTS, required=True)
    parser.add_argument('--expected-staging-version', type=int)
    args = parser.parse_args()
    if args.environment == 'production' and not args.expected_staging_version:
        parser.error('production requires --expected-staging-version')
    if args.environment == 'staging' and args.expected_staging_version is not None:
        parser.error('staging does not accept --expected-staging-version')
    path = Path(os.environ.get('RUNPOD_UPDATE_REPORT', 'runpod-update.json'))
    report = {'status': 'preflight', 'environment': args.environment}
    def save():
        path.write_text(json.dumps(report, indent=2) + '\n')
    def interrupted(signum, frame):
        raise RuntimeError('RunPod update interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    save()
    try:
        promote(args.environment, args.url, report, save, args.expected_staging_version)
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
