#!/usr/bin/env python3
"""Promote an attested staging release to production without changing runtime configuration."""
import argparse
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid
import zipfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('staging_release', ROOT / 'scripts/deploy-v2-staging-release.py')
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
aws, api = common.aws, common.api
ACCOUNT, REGION = common.ai.ACCOUNT, common.ai.REGION
CLUSTER = 'yeodam-v2-production-app'
SERVICES = {'frontend': 'yeodam-v2-production-frontend', 'backend': 'yeodam-v2-production-backend',
            'ai-app': 'yeodam-v2-production-ai-api'}
CONTAINERS = {'frontend': 'frontend', 'backend': 'backend', 'ai-app': 'ai-api'}
TARGETS = {'frontend': 'yeodam-v2-production-fe', 'backend': 'yeodam-v2-production-be',
           'ai-app': 'yeodam-v2-prod-ai-api'}
ORDER = ['ai-app', 'backend', 'frontend']
INSTANCE = 'i-05e1ad9dd96dba4ae'
DOCUMENT = 'yeodam-v2-production-ai-runner-deploy'
LAST, PREVIOUS, ACTIVE = [f'/yeodam/v2/production/deploy/{s}' for s in ['last-success', 'previous-success', 'active']]


def now():
    return datetime.now(timezone.utc).isoformat()


def staging_candidate(run_id):
    if not re.fullmatch('[1-9][0-9]*', str(run_id)):
        raise ValueError('A successful staging deployment Run ID is required')
    run = api(f'repos/{common.CLOUD}/actions/runs/{run_id}')
    if (run['path'] != '.github/workflows/v2-staging-release.yml' or run['head_branch'] != 'main'
            or run['head_repository']['full_name'] != common.CLOUD
            or run['event'] not in {'push', 'workflow_dispatch'}
            or run['status'] != 'completed' or run['conclusion'] != 'success'):
        raise ValueError('Only a successful protected-main staging deployment can be promoted')
    artifacts = api(f'repos/{common.CLOUD}/actions/runs/{run_id}/artifacts?per_page=100')['artifacts']
    name = f"v2-staging-release-{run_id}-{run['run_attempt']}"
    matches = [a for a in artifacts if a['name'] == name and not a['expired']]
    if len(matches) != 1 or matches[0]['size_in_bytes'] > 1024 * 1024:
        raise ValueError('Latest-attempt staging release evidence is absent, expired or ambiguous')
    blob = subprocess.run(['gh', 'api', f"repos/{common.CLOUD}/actions/artifacts/{matches[0]['id']}/zip"],
                          check=True, stdout=subprocess.PIPE).stdout
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        rows = [f for f in archive.infolist() if f.filename == 'release.json']
        if len(rows) != 1 or rows[0].file_size > 65536:
            raise ValueError('Invalid staging release report')
        report = json.loads(archive.read(rows[0]))
    if report.get('status') != 'healthy' or report.get('schemaVersion') != 1:
        raise ValueError('Staging did not attest a healthy whole release')
    commit = report['cloudCommit']
    if not re.fullmatch('[0-9a-f]{40}', commit) or (run['event'] == 'push' and run['head_sha'] != commit):
        raise ValueError('Staging evidence does not match its triggering commit')
    data = common.manifest_at(commit)
    # Bind evidence to its merged candidate and the exact successful CI Run.
    pr = api(f"repos/{common.CLOUD}/pulls/{report['pr']}")
    if (not pr['merged'] or pr['merge_commit_sha'] != commit or pr['base']['ref'] != 'main'
            or pr['base']['repo']['full_name'] != common.CLOUD
            or (pr['head'].get('repo') or {}).get('full_name') != common.CLOUD):
        raise ValueError('Staging report does not identify a merged Cloud candidate')
    if data != common.manifest_at(pr['head']['sha']):
        raise ValueError('Promoted manifest differs from the tested PR head')
    ci = api(f"repos/{common.CLOUD}/actions/runs/{report['ciRunId']}")
    if (ci['path'] != '.github/workflows/cloud-pr-gate.yml' or ci['event'] != 'pull_request'
            or ci['head_sha'] != pr['head']['sha'] or ci['status'] != 'completed'
            or ci['conclusion'] != 'success' or ci['head_repository']['full_name'] != common.CLOUD):
        raise ValueError('Candidate CI evidence is not successful or does not match')
    jobs = api(f"repos/{common.CLOUD}/actions/runs/{ci['id']}/jobs?filter=latest&per_page=100")['jobs']
    for required in ['V2 image combination smoke', 'cloud-ci-gate']:
        matched = [j for j in jobs if j['name'] == required]
        if len(matched) != 1 or matched[0]['conclusion'] != 'success':
            raise ValueError('Required candidate CI job not successful: ' + required)
    return {'stagingRunId': int(run_id), 'stagingAttempt': run['run_attempt'], 'cloudCommit': commit,
            'ciRunId': ci['id'], 'pr': report['pr'], 'manifest': data}


def config_hash(definition):
    data = {k: copy.deepcopy(v) for k, v in definition.items() if k in common.ai.FIELDS and v is not None}
    for c in data['containerDefinitions']:
        c.pop('image', None)
        c['environment'] = sorted([e for e in c.get('environment', []) if e['name'] != 'RELEASE'],
                                  key=lambda e: e['name'])
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def definition(name, arn):
    row = aws('ecs', 'describe-task-definition', '--task-definition', arn)['taskDefinition']
    if (row['status'] != 'ACTIVE' or row['family'] != SERVICES[name]
            or [c['name'] for c in row['containerDefinitions']] != [CONTAINERS[name]]):
        raise ValueError('Unexpected production task definition: ' + name)
    common.normalize_image(name, row['containerDefinitions'][0]['image'])
    return row


def snapshot():
    result = aws('ecs', 'describe-services', '--cluster', CLUSTER, '--services', *SERVICES.values())
    if result.get('failures') or len(result.get('services', [])) != 3:
        raise RuntimeError('All three production ECS services are required')
    images, revisions, configs = {}, {}, {}
    for name, service_name in SERVICES.items():
        s = next(s for s in result['services'] if s['serviceName'] == service_name)
        if not common.ai.stable(s, s['taskDefinition']):
            raise RuntimeError('Production service is not stable: ' + name)
        d = definition(name, s['taskDefinition'])
        images[name] = common.normalize_image(name, d['containerDefinitions'][0]['image'])
        revisions[name], configs[name] = s['taskDefinition'], config_hash(d)
        verify_targets(name, s)
    return {'images': images, 'revisions': revisions, 'configs': configs}


def verify_targets(name, service):
    lbs = service.get('loadBalancers', [])
    prefix = f'arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/{TARGETS[name]}/'
    if len(lbs) != 1 or not lbs[0]['targetGroupArn'].startswith(prefix):
        raise ValueError('Unexpected production target group')
    ids = aws('ecs', 'list-tasks', '--cluster', CLUSTER, '--service-name', SERVICES[name])['taskArns']
    if not ids:
        raise RuntimeError('No running production tasks')
    tasks = aws('ecs', 'describe-tasks', '--cluster', CLUSTER, '--tasks', *ids)
    if tasks.get('failures'):
        raise RuntimeError('Cannot attest production tasks')
    rows = [t for t in tasks['tasks'] if t['taskDefinitionArn'] == service['taskDefinition']
            and t['lastStatus'] == 'RUNNING' and t.get('healthStatus') == 'HEALTHY']
    health = aws('elbv2', 'describe-target-health', '--target-group-arn', lbs[0]['targetGroupArn'])['TargetHealthDescriptions']
    ips = {n['privateIpv4Address'] for t in rows for c in t['containers'] for n in c.get('networkInterfaces', [])}
    healthy = {t['Target']['Id'] for t in health if t['TargetHealth']['State'] == 'healthy'}
    if len(rows) < service['desiredCount'] or not ips.issubset(healthy):
        raise RuntimeError('Current production Task/ALB health is incomplete: ' + name)


def register(name, current, image):
    image = common.normalize_image(name, image)
    data = {k: copy.deepcopy(v) for k, v in current.items() if k in common.ai.FIELDS and v is not None}
    c = data['containerDefinitions'][0]
    c['image'] = image
    c['environment'] = [e for e in c.get('environment', []) if e['name'] != 'RELEASE'] + [{'name': 'RELEASE', 'value': image}]
    if config_hash(current) != config_hash(data):
        raise ValueError('Production runtime configuration changed')
    return aws('ecs', 'register-task-definition', '--cli-input-json', json.dumps(data))['taskDefinition']['taskDefinitionArn']


def update(name, revision):
    aws('ecs', 'update-service', '--cluster', CLUSTER, '--service', SERVICES[name], '--task-definition', revision)
    for _ in range(90):
        result = aws('ecs', 'describe-services', '--cluster', CLUSTER, '--services', SERVICES[name])
        if result.get('failures'):
            raise RuntimeError('Production service lookup failed')
        s = result['services'][0]
        if common.ai.stable(s, revision):
            verify_targets(name, s)
            return
        if any(d.get('rolloutState') == 'FAILED' and d['taskDefinition'] == revision for d in s.get('deployments', [])):
            raise RuntimeError('Production deployment circuit breaker failed: ' + name)
        time.sleep(10)
    raise TimeoutError('Production rollout timed out: ' + name)


def runner(action='inspect', image=''):
    try:
        command = aws('ssm', 'send-command', '--instance-ids', INSTANCE, '--document-name', DOCUMENT,
                      '--timeout-seconds', '60', '--parameters', json.dumps({'Action': [action], 'ImageDigest': [image]}))
    except RuntimeError as error:
        raise TimeoutError('SSM outcome unknown: submission response unavailable; inspect command history') from error
    command_id = command['Command']['CommandId']
    for _ in range(120):
        try:
            row = aws('ssm', 'get-command-invocation', '--command-id', command_id, '--instance-id', INSTANCE)
        except RuntimeError as error:
            if 'InvocationDoesNotExist' not in str(error):
                raise TimeoutError('SSM outcome unknown: ' + command_id) from error
            time.sleep(5)
            continue
        if row['Status'] == 'Success':
            data = json.loads(row['StandardOutputContent'].strip())
            data['image'] = common.normalize_image('ai-app', data['image'])
            if data.get('health') != 'healthy' or data['image'] != data.get('configuredImage'):
                raise RuntimeError('Production runner image/readiness mismatch')
            if action == 'deploy' and data['image'] != image:
                raise RuntimeError('Production runner did not adopt the selected image')
            return data
        if row['Status'] in {'Failed', 'Cancelled', 'TimedOut', 'Cancelling'}:
            # An agent timeout/cancellation may leave the remote process alive.
            if row['Status'] != 'Failed':
                raise TimeoutError('SSM outcome unknown: ' + command_id)
            raise RuntimeError('Production runner SSM failed: ' + command_id)
        time.sleep(5)
    raise TimeoutError('SSM outcome unknown: ' + command_id)


def get_state(path):
    try:
        value = aws('ssm', 'get-parameter', '--name', path)['Parameter']['Value']
    except RuntimeError as error:
        if 'ParameterNotFound' in str(error):
            return None
        raise
    data = json.loads(value)
    if data.get('schemaVersion') != 1:
        raise ValueError('Unsupported production deployment state')
    return data


def put_state(path, data):
    value = json.dumps(data, separators=(',', ':'))
    if len(value.encode()) > 4096:
        raise ValueError('Production state exceeds Standard Parameter limit')
    aws('ssm', 'put-parameter', '--name', path, '--type', 'String', '--tier', 'Standard', '--overwrite', '--value', value)


def promote(selected, action, apply, directory, redeploy_frontend=False):
    report = {'schemaVersion': 1, 'releaseId': str(uuid.uuid4()), 'status': 'checking', 'action': action,
              'startedAt': now(), **{k: v for k, v in selected.items() if k != 'manifest'}}
    directory.mkdir(parents=True, exist_ok=True)
    def save():
        (directory / 'release.json').write_text(json.dumps(report, indent=2) + '\n')
    save()
    if os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', REGION)) != REGION:
        raise ValueError('Wrong AWS Region')
    if aws('sts', 'get-caller-identity')['Account'] != ACCOUNT:
        raise ValueError('Wrong AWS Account')
    active, last = get_state(ACTIVE), get_state(LAST)
    if active and active['status'] not in {'healthy', 'restored_previous'}:
        raise RuntimeError('Production deployment journal needs review: ' + active['releaseId'])
    before = snapshot()
    desired = common.image_refs(selected['manifest'])
    if last:
        if before['images'] != common.image_refs(last['manifest']):
            raise RuntimeError('Production drift from last successful image combination')
    elif before['images'] != desired:
        raise RuntimeError('First run must attest the currently deployed staging-approved combination')
    changes = [n for n in ORDER if before['images'][n] != desired[n] or (n == 'frontend' and redeploy_frontend)]
    report.update(changed=changes, before=before)
    if not apply:
        report['status'] = 'planned'
        save()
        return report
    # Verify runner even on initial/no-op releases; never power-cycle the shared host.
    previous_runner = runner()['image']
    if previous_runner != before['images']['ai-app']:
        raise RuntimeError('Production API/runner images differ before deployment')
    report['status'] = 'deploying'
    save()
    put_state(ACTIVE, report)
    attempted, unknown = [], False
    try:
        for name in changes:
            report['phase'] = name
            save()
            put_state(ACTIVE, report)
            current = definition(name, before['revisions'][name])
            revision = register(name, current, desired[name])
            attempted.append(name)
            update(name, revision)
            if name == 'ai-app':
                report['phase'] = 'production_runner'
                save()
                put_state(ACTIVE, report)
                runner('deploy', desired[name])
        after = snapshot()
        if after['images'] != desired or after['configs'] != before['configs']:
            raise RuntimeError('Production images or preserved configuration differ')
        if runner()['image'] != desired['ai-app']:
            raise RuntimeError('Production runner release mismatch')
    except Exception as error:
        unknown = isinstance(error, TimeoutError) and 'SSM outcome unknown' in str(error)
        errors = []
        if unknown:
            errors.append('SSM outcome unknown; inspect command before any competing recovery')
        else:
            for name in reversed(attempted):
                try:
                    # Restore only images, preserving the predeployment configuration.
                    update(name, before['revisions'][name])
                    if name == 'ai-app':
                        runner('deploy', previous_runner)
                except Exception as recovery:
                    errors.append(name + ': ' + str(recovery))
            try:
                restored = snapshot()
                if restored['images'] != before['images'] or restored['configs'] != before['configs']:
                    errors.append('Previous production image/configuration verification failed')
                if runner()['image'] != previous_runner:
                    errors.append('Previous production runner image verification failed')
            except Exception as recovery:
                errors.append(str(recovery))
        report.update(status='recovery_required' if errors else 'restored_previous', failure=str(error), recoveryErrors=errors)
        save()
        put_state(ACTIVE, report)
        raise
    state = {'schemaVersion': 1, 'releaseId': report['releaseId'], **selected,
             'revisions': after['revisions'], 'runnerImage': desired['ai-app'], 'completedAt': now()}
    # Ambiguous state finalization must never roll back otherwise healthy apps.
    try:
        if last and common.image_refs(last['manifest']) != desired:
            put_state(PREVIOUS, last)
        put_state(LAST, state)
        report.update(status='healthy', after=after, completedAt=state['completedAt'])
        save()
        # The persisted journal excludes bulky snapshots and contains no credentials.
        put_state(ACTIVE, {k: v for k, v in report.items() if k not in {'before', 'after'}})
    except Exception as error:
        report.update(status='recovery_required', failure='Healthy apps; state finalization needs review: ' + str(error))
        save()
        raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['deploy', 'rollback'])
    parser.add_argument('--staging-run', help='Successful V2 staging deployment Run ID')
    parser.add_argument('--apply', action='store_true', help='Default: read-only plan')
    parser.add_argument('--redeploy-frontend', action='store_true', help='Recreate FE only, even at the same Digest')
    args = parser.parse_args()
    if args.action == 'deploy':
        selected = staging_candidate(args.staging_run)
    else:
        if args.staging_run or args.redeploy_frontend:
            raise ValueError('Rollback selects previous-success; do not pass staging Run or redeploy options')
        previous = get_state(PREVIOUS)
        if not previous:
            raise ValueError('No previous successful production release')
        # A previously attested successful release remains recoverable after the
        # staging artifact's retention expires. Only this role writes the record.
        common.manifest.validate(previous['manifest'])
        selected = {k: previous[k] for k in ['stagingRunId', 'stagingAttempt', 'cloudCommit', 'ciRunId', 'pr', 'manifest']}
    report = promote(selected, args.action, args.apply,
                     Path(os.environ.get('RELEASE_REPORT_DIR', '/private/tmp/yeodam-v2-production-release')),
                     args.redeploy_frontend)
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
