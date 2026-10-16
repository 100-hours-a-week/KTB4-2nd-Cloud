#!/usr/bin/env python3
"""Deploy a staging AI API/runner image pair, or restore an active API revision."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

ACCOUNT = '483175530259'
REGION = 'ap-northeast-2'
CLUSTER = 'yeodam-v2-staging-app'
SERVICE = FAMILY = 'yeodam-v2-staging-ai-api'
INSTANCE = 'i-05e1ad9dd96dba4ae'
DOCUMENT = 'yeodam-v2-staging-ai-runner-deploy'
TARGET = 'yeodam-v2-stg-ai-api'
PACKAGE = 'ghcr.io/100-hours-a-week/yeodam-ai-app'
FIELDS = ('family taskRoleArn executionRoleArn networkMode containerDefinitions volumes '
          'placementConstraints requiresCompatibilities cpu memory pidMode ipcMode proxyConfiguration '
          'inferenceAccelerators ephemeralStorage runtimePlatform enableFaultInjection').split()


def image_ref(value):
    if not re.fullmatch(re.escape(PACKAGE) + r'(?::sha-[0-9a-f]{40})?@sha256:[0-9a-f]{64}', value):
        raise ValueError('Expected an immutable yeodam-ai-app image')
    return PACKAGE + '@' + value.rsplit('@', 1)[1]


def aws(*args):
    result = subprocess.run(['aws', '--region', REGION, '--output', 'json', *args],
                            capture_output=True, text=True)
    if result.returncode:
        # These calls contain only deployment identifiers, never Secret values.
        raise RuntimeError('AWS ' + ' '.join(args[:2]) + ' failed: ' + result.stderr.strip()[:700])
    return json.loads(result.stdout) if result.stdout.strip() else {}


def service():
    data = aws('ecs', 'describe-services', '--cluster', CLUSTER, '--services', SERVICE)
    if data.get('failures') or len(data.get('services', [])) != 1:
        raise RuntimeError('Staging AI service not found')
    return data['services'][0]


def stable(data, expected):
    deployments = data.get('deployments', [])
    return (data.get('status') == 'ACTIVE' and data.get('taskDefinition') == expected
            and data.get('desiredCount', 0) > 0 and data.get('runningCount') == data['desiredCount']
            and data.get('pendingCount') == 0 and len(deployments) == 1
            and deployments[0].get('status') == 'PRIMARY'
            and deployments[0].get('rolloutState') == 'COMPLETED')


def definition(arn):
    row = aws('ecs', 'describe-task-definition', '--task-definition', arn)['taskDefinition']
    if (row['family'] != FAMILY or row['status'] != 'ACTIVE'
            or [c['name'] for c in row['containerDefinitions']] != ['ai-api']):
        raise ValueError('Not an active staging AI API definition')
    image_ref(row['containerDefinitions'][0]['image'])
    return row


def candidate_definition(current, image):
    data = {key: value for key, value in current.items() if key in FIELDS and value is not None}
    data = json.loads(json.dumps(data))
    container = data['containerDefinitions'][0]
    container['image'] = image_ref(image)
    container['environment'] = [v for v in container.get('environment', []) if v['name'] != 'RELEASE']
    container['environment'].append({'name': 'RELEASE', 'value': image_ref(image)})
    return data


def runner(action, image=''):
    command = aws('ssm', 'send-command', '--instance-ids', INSTANCE, '--document-name', DOCUMENT,
                  '--timeout-seconds', '60', '--parameters', json.dumps({'Action': [action], 'ImageDigest': [image]}))
    command_id = command['Command']['CommandId']
    for _ in range(120):
        try:
            result = aws('ssm', 'get-command-invocation', '--command-id', command_id, '--instance-id', INSTANCE)
        except RuntimeError as error:
            if 'InvocationDoesNotExist' not in str(error):
                raise
            time.sleep(5)
            continue
        status = result['Status']
        if status == 'Success':
            row = json.loads(result['StandardOutputContent'].strip())
            row['image'] = image_ref(row['image'])
            if action == 'deploy' and (row.get('health') != 'healthy' or row['image'] != image_ref(image)):
                raise RuntimeError('Runner returned an unexpected release or health')
            return row
        if status in {'Failed', 'Cancelled', 'TimedOut', 'Cancelling'}:
            raise RuntimeError(f'Runner SSM command {command_id} {status}; inspect this command')
        time.sleep(5)
    # Never start another host update while the first command may still be running.
    raise TimeoutError(f'Runner command {command_id} status unknown; inspect before recovery')


def wait_api(expected, target):
    for _ in range(90):
        row = service()
        if stable(row, expected):
            targets = aws('elbv2', 'describe-target-health', '--target-group-arn', target)['TargetHealthDescriptions']
            if sum(t['TargetHealth']['State'] == 'healthy' for t in targets) >= row['desiredCount']:
                return
        time.sleep(10)
    raise TimeoutError('AI ECS rollout or ALB health timed out')


def update_api(arn, target):
    aws('ecs', 'update-service', '--cluster', CLUSTER, '--service', SERVICE, '--task-definition', arn)
    wait_api(arn, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['deploy', 'rollback'])
    parser.add_argument('value', help='Immutable image for deploy; active API revision number for rollback')
    args = parser.parse_args()
    if args.action == 'deploy':
        image_ref(args.value)
        if ':sha-' in args.value:
            raise ValueError('Deploy accepts package@sha256 only')
    elif not re.fullmatch(r'[1-9][0-9]*', args.value):
        raise ValueError('Rollback needs a positive API revision number')
    if os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', REGION)) != REGION:
        raise ValueError('Wrong AWS Region')
    if aws('sts', 'get-caller-identity')['Account'] != ACCOUNT:
        raise ValueError('Wrong AWS Account')
    instance = aws('ec2', 'describe-instances', '--instance-ids', INSTANCE)['Reservations'][0]['Instances'][0]
    if instance['State']['Name'] != 'running':
        raise RuntimeError('GPU is stopped; explicitly start it before deploying. CD does not start GPUs.')
    current = service()
    previous = current['taskDefinition']
    if args.action == 'deploy' and not stable(current, previous):
        raise RuntimeError('AI API must be stable before deployment; inspect ECS events first')
    before = definition(previous)
    previous_image = image_ref(before['containerDefinitions'][0]['image'])
    runner_before = runner('inspect')
    old_runner = runner_before['image']
    if args.action == 'deploy' and (old_runner != previous_image or runner_before.get('configuredImage') != old_runner or runner_before.get('health') != 'healthy'):
        raise RuntimeError('API/runner release or readiness mismatch: ' + json.dumps({'apiImage': previous_image, 'runner': runner_before}))
    target = aws('elbv2', 'describe-target-groups', '--names', TARGET)['TargetGroups'][0]['TargetGroupArn']
    if not target.startswith(f'arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/{TARGET}/'):
        raise ValueError('Unexpected target group')
    if args.action == 'rollback':
        candidate = f'arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{FAMILY}:{args.value}'
        desired = definition(candidate)
        if any(desired.get(key) != before.get(key) for key in ['taskRoleArn', 'executionRoleArn']):
            raise ValueError('Rollback task roles differ from current staging roles')
        image = image_ref(desired['containerDefinitions'][0]['image'])
    else:
        image = image_ref(args.value)
        # Terraform may have registered a newer runtime configuration without moving
        # the CD-owned service. Promote that config instead of cloning stale envs.
        template = definition(FAMILY)
        if any(template.get(key) != before.get(key) for key in ['taskRoleArn', 'executionRoleArn']):
            raise ValueError('Latest task roles differ; review the infrastructure change first')
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json') as file:
            json.dump(candidate_definition(template, image), file)
            file.flush()
            candidate = aws('ecs', 'register-task-definition', '--cli-input-json', 'file://' + file.name)['taskDefinition']['taskDefinitionArn']
    report = {'action': args.action, 'previousRevision': previous, 'previousRunnerImage': old_runner,
              'candidateRevision': candidate, 'candidateImage': image, 'status': 'deploying'}
    report_path = Path(os.environ.get('AI_DEPLOY_REPORT', 'ai-deployment.json'))
    def save():
        report_path.write_text(json.dumps(report, indent=2) + '\n')
    save()
    print(json.dumps(report), flush=True)
    try:
        update_api(candidate, target)
        report['phase'] = 'runner'
        save()
        runner('deploy', image)
        wait_api(candidate, target)
        report['status'] = 'healthy'
    except Exception as error:
        report['failure'] = str(error)
        report['status'] = 'failed'
        save()
        if isinstance(error, TimeoutError) and 'status unknown' in str(error):
            raise  # Unknown SSM outcome: do not race another runner mutation.
        recovery_errors = []
        try:
            update_api(previous, target)
        except Exception as recovery:
            recovery_errors.append('API: ' + str(recovery))
        try:
            runner('deploy', old_runner)
        except Exception as recovery:
            recovery_errors.append('runner: ' + str(recovery))
        report['status'] = 'recovery_required' if recovery_errors else 'restored_previous'
        if recovery_errors:
            report['recoveryErrors'] = recovery_errors
        save()
        raise
    save()
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
