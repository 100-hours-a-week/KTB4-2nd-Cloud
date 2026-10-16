#!/usr/bin/env python3
"""Promote a CI-verified merged V2 manifest to staging; never deploy production."""
import argparse
import base64
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
CLOUD = '100-hours-a-week/KTB4-2nd-Cloud'
MANIFEST = 'deploy/v2/image-manifest.json'
LAST = '/yeodam/v2/staging/deploy/last-success'
ACTIVE = '/yeodam/v2/staging/deploy/active'
SERVICES = {'frontend': 'yeodam-v2-staging-frontend', 'backend': 'yeodam-v2-staging-backend', 'ai-app': 'yeodam-v2-staging-ai-api'}
TARGETS = {'frontend': 'yeodam-v2-staging-fe', 'backend': 'yeodam-v2-staging-be', 'ai-app': 'yeodam-v2-stg-ai-api'}
ORDER = ['ai-app', 'backend', 'frontend']


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manifest = load_script('v2-image-manifest')
ai = load_script('deploy-v2-staging-ai')
aws = ai.aws


def now():
    return datetime.now(timezone.utc).isoformat()


def api(path):
    result = subprocess.run(['gh', 'api', path], check=True, text=True, stdout=subprocess.PIPE)
    return json.loads(result.stdout)


def manifest_at(ref):
    if ref != 'main' and not re.fullmatch(r'[0-9a-f]{40}', ref):
        raise ValueError('Invalid Cloud ref')
    file = api(f'repos/{CLOUD}/contents/{MANIFEST}?ref={ref}')
    return manifest.validate(json.loads(base64.b64decode(file['content']), object_pairs_hook=manifest.unique_object))


def merged_pr_for_commit(commit):
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Invalid pushed Cloud commit')
    prs = api(f'repos/{CLOUD}/commits/{commit}/pulls?per_page=100')
    matches = [pr for pr in prs if pr.get('merged_at') and pr.get('merge_commit_sha') == commit
               and pr['base']['ref'] == 'main' and pr['base']['repo']['full_name'] == CLOUD
               and (pr['head'].get('repo') or {}).get('full_name') == CLOUD]
    if len(matches) != 1:
        raise ValueError('Pushed commit must identify exactly one merged Cloud-to-main PR')
    return str(matches[0]['number'])


def candidate(number):
    if not re.fullmatch(r'[1-9][0-9]*', str(number)):
        raise ValueError('A merged Cloud PR number is required')
    pr = api(f'repos/{CLOUD}/pulls/{number}')
    if (not pr.get('merged') or pr['base']['ref'] != 'main'
            or pr['base']['repo']['full_name'] != CLOUD or pr['head']['repo']['full_name'] != CLOUD):
        raise ValueError('Only merged Cloud-to-main PRs can be promoted')
    merge = pr['merge_commit_sha']
    head = pr['head']['sha']
    desired = manifest_at(merge)
    if desired != manifest_at('main'):
        return None  # A newer whole manifest supersedes this queued delivery.
    if desired != manifest_at(head):
        raise ValueError('Merged manifest differs from the PR candidate')
    runs = api(f'repos/{CLOUD}/actions/workflows/cloud-pr-gate.yml/runs?head_sha={head}&event=pull_request&per_page=100')['workflow_runs']
    runs = [r for r in runs if r['head_sha'] == head and r['head_branch'] == pr['head']['ref']
            and r['event'] == 'pull_request' and r['path'] == '.github/workflows/cloud-pr-gate.yml'
            and r['head_repository']['full_name'] == CLOUD]
    if not runs:
        raise ValueError('No matching Cloud PR Gate Run')
    run = max(runs, key=lambda r: r['id'])
    if run['status'] != 'completed' or run['conclusion'] != 'success':
        raise ValueError('Latest matching Cloud PR Gate did not succeed')
    # Merged runs can have pull_requests=[]; bind by exact head SHA/ref instead.
    jobs = api(f"repos/{CLOUD}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100")['jobs']
    for name in ['V2 image combination smoke', 'cloud-ci-gate']:
        matches = [j for j in jobs if j['name'] == name]
        if len(matches) != 1 or matches[0]['conclusion'] != 'success':
            raise ValueError('Required CI job not successful: ' + name)
    return {'pr': int(number), 'cloudCommit': merge, 'ciRunId': run['id'], 'manifest': desired}


def image_refs(data):
    return {name: entry['image'].split(':sha-', 1)[0] + '@' + entry['digest']
            for name, entry in manifest.validate(data)['components'].items()}


def normalize_image(name, value):
    package = 'ghcr.io/100-hours-a-week/' + manifest.COMPONENTS[name][1]
    if not re.fullmatch(re.escape(package) + r'(?::sha-[0-9a-f]{40})?@sha256:[0-9a-f]{64}', value):
        raise ValueError('Unexpected staging image for ' + name)
    return package + '@' + value.rsplit('@', 1)[1]


def snapshot():
    result = aws('ecs', 'describe-services', '--cluster', ai.CLUSTER, '--services', *SERVICES.values())
    if result.get('failures') or len(result.get('services', [])) != 3:
        raise RuntimeError('All three staging ECS services are required')
    targets = aws('elbv2', 'describe-target-groups', '--names', *TARGETS.values())['TargetGroups']
    targets = {t['TargetGroupName']: t['TargetGroupArn'] for t in targets}
    images, revisions = {}, {}
    for name, service_name in SERVICES.items():
        service = next(s for s in result['services'] if s['serviceName'] == service_name)
        revision = service['taskDefinition']
        if not ai.stable(service, revision):
            raise RuntimeError('Service is not stable: ' + service_name)
        definition = aws('ecs', 'describe-task-definition', '--task-definition', revision)['taskDefinition']
        if definition['family'] != service_name or len(definition['containerDefinitions']) != 1:
            raise ValueError('Unexpected staging task definition')
        images[name] = normalize_image(name, definition['containerDefinitions'][0]['image'])
        revisions[name] = revision
        health = aws('elbv2', 'describe-target-health', '--target-group-arn', targets[TARGETS[name]])['TargetHealthDescriptions']
        if sum(t['TargetHealth']['State'] == 'healthy' for t in health) < service['desiredCount']:
            raise RuntimeError('ALB targets are not healthy: ' + service_name)
    return {'images': images, 'revisions': revisions}


def get_state(name):
    try:
        result = aws('ssm', 'get-parameter', '--name', name)
    except RuntimeError as error:
        if 'ParameterNotFound' in str(error):
            return None
        raise
    data = json.loads(result['Parameter']['Value'])
    if data.get('schemaVersion') != 1:
        raise ValueError('Unsupported deployment state schema')
    return data


def put_state(name, data):
    # No secrets. Standard Parameter size stays bounded; never switch to Advanced.
    value = json.dumps(data, separators=(',', ':'))
    if len(value.encode()) > 4096:
        raise ValueError('Deployment state exceeds Standard Parameter limit')
    aws('ssm', 'put-parameter', '--name', name, '--type', 'String', '--tier', 'Standard', '--overwrite', '--value', value)


def gpu_state():
    return aws('ec2', 'describe-instances', '--instance-ids', ai.INSTANCE)['Reservations'][0]['Instances'][0]['State']['Name']


def ready_runner():
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        rows = aws('ssm', 'describe-instance-information', '--filters',
                   json.dumps([{'Key': 'InstanceIds', 'Values': [ai.INSTANCE]}]))['InstanceInformationList']
        if rows and rows[0]['PingStatus'] == 'Online':
            try:
                row = ai.runner('inspect')
                if row.get('health') == 'healthy' and row['image'] == row.get('configuredImage'):
                    return row['image']
            except RuntimeError:
                pass  # Cached model and containers may still be starting after EC2 boot.
        time.sleep(10)
    raise TimeoutError('GPU runner readiness timed out; inspect SSM and runtime dependencies')


def changed_services(before, desired):
    return [name for name in ORDER if before['images'][name] != desired[name]]


def run_service(name, action, value, report_dir):
    if name == 'ai-app':
        cmd = ['python3', str(ROOT / 'scripts/deploy-v2-staging-ai.py'), action, value]
    else:
        cmd = ['bash', str(ROOT / f'scripts/deploy-v2-staging-{name}.sh'), action, value]
    ai_report = report_dir / f'ai-{action}.json'
    if name == 'ai-app':
        ai_report.unlink(missing_ok=True)
    subprocess.run(cmd, check=True, env=os.environ | {'AI_DEPLOY_REPORT': str(ai_report)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--pr', help='Merged PR for manual retry')
    source.add_argument('--commit', help='Exact main push commit for automatic deployment')
    parser.add_argument('--apply', action='store_true', help='Default is read-only planning')
    args = parser.parse_args()
    report_dir = Path(os.environ.get('RELEASE_REPORT_DIR', '/private/tmp/yeodam-v2-release'))
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {'schemaVersion': 1, 'releaseId': str(uuid.uuid4()), 'status': 'checking', 'startedAt': now()}
    def save():
        (report_dir / 'release.json').write_text(json.dumps(report, indent=2) + '\n')
    save()
    selected = candidate(merged_pr_for_commit(args.commit) if args.commit else args.pr)
    if selected is not None and args.commit and selected['cloudCommit'] != args.commit:
        raise ValueError('Merged PR does not match the pushed commit')
    if selected is None:
        report['status'] = 'superseded'
        save()
        return
    if aws('sts', 'get-caller-identity')['Account'] != ai.ACCOUNT:
        raise ValueError('Wrong AWS account')
    before = snapshot()
    desired = image_refs(selected['manifest'])
    last = get_state(LAST)
    active = get_state(ACTIVE)
    if last and (last.get('runnerImage') != image_refs(last['manifest'])['ai-app'] or not isinstance(last.get('runnerVerifiedAt'), str)):
        raise ValueError('Last successful runner evidence is incomplete')
    if active and active['status'] in {'deploying', 'recovery_required'}:
        raise RuntimeError('Unfinished deployment journal requires recovery review: ' + active['releaseId'])
    if last and before['images'] != image_refs(last['manifest']) and before['images'] != desired:
        raise RuntimeError('Manual deployment drift: restore the last successful combination or reconcile the approved manifest')
    changes = changed_services(before, desired)
    verify_runner = not last or 'ai-app' in changes or before['images'] != image_refs(last['manifest'])
    initial_gpu = gpu_state() if verify_runner else None
    report.update({key: value for key, value in selected.items() if key != 'manifest'})
    report.update(changed=changes, before=before, gpuInitialState=initial_gpu, needsRunnerVerification=verify_runner)
    if not args.apply:
        report['status'] = 'planned'
        save()
        print(json.dumps(report), flush=True)
        return
    # Recheck the desired whole manifest immediately before any write.
    if selected['manifest'] != manifest_at('main'):
        report['status'] = 'superseded'
        save()
        return
    report['status'] = 'deploying'
    save()
    put_state(ACTIVE, report)
    attempted, started_gpu, unknown_runner = [], False, False
    try:
        if verify_runner:
            report['phase'] = 'runner_preflight'
            save()
            put_state(ACTIVE, report)
            print(json.dumps({'phase': 'runner_preflight', 'gpuInitialState': initial_gpu}), flush=True)
            if initial_gpu == 'stopped':
                started_gpu = True
                report['gpuStartedByCd'] = True
                save()
                aws('ec2', 'start-instances', '--instance-ids', ai.INSTANCE)
            elif initial_gpu != 'running':
                raise RuntimeError('GPU is transitioning; retry after it is running or stopped')
            prior_runner = ready_runner()
            if prior_runner != before['images']['ai-app']:
                raise RuntimeError('API and runner images differ before deployment; restore the AI pair first')
        for name in changes:
            report['phase'] = name
            print(json.dumps({'phase': name, 'image': desired[name]}), flush=True)
            save()
            put_state(ACTIVE, report)
            attempted.append(name)  # Include a partially mutated service on failure.
            run_service(name, 'deploy', desired[name], report_dir)
        after = snapshot()
        if after['images'] != desired:
            raise RuntimeError('Runtime images differ from the approved manifest')
        runner_image = ready_runner() if verify_runner else last['runnerImage']
        if runner_image != desired['ai-app']:
            raise RuntimeError('Runner image differs from approved AI image')
    except Exception as error:
        report.update(status='failed', failure=str(error))
        ai_report = report_dir / 'ai-deploy.json'
        if ai_report.exists():
            unknown_runner = 'status unknown' in json.loads(ai_report.read_text()).get('failure', '')
        errors = []
        for name in reversed(attempted):
            if name == 'ai-app' and unknown_runner:
                errors.append('AI SSM outcome unknown; no competing mutation started')
                continue
            try:
                run_service(name, 'rollback', before['revisions'][name].rsplit(':', 1)[1], report_dir)
            except Exception as recovery:
                errors.append(name + ': ' + str(recovery))
        try:
            if snapshot()['images'] != before['images']:
                errors.append('Runtime rollback image mismatch')
        except Exception as recovery:
            errors.append(str(recovery))
        report['status'] = 'recovery_required' if errors else 'restored_previous'
        report['recoveryErrors'] = errors
        save()
        put_state(ACTIVE, report)
        raise
    else:
        state = {'schemaVersion': 1, 'releaseId': report['releaseId'], **selected,
                 'revisions': after['revisions'], 'runnerImage': runner_image,
                 'runnerVerifiedAt': now() if verify_runner else last['runnerVerifiedAt'], 'completedAt': now()}
        # A state write's ambiguous outcome must not trigger a rollback of healthy apps.
        try:
            put_state(LAST, state)
            report.update(status='healthy', completedAt=state['completedAt'])
            save()
            put_state(ACTIVE, report)
        except Exception as error:
            report.update(status='recovery_required', failure='Apps healthy; state finalization needs review: ' + str(error))
            save()
            raise
    finally:
        if started_gpu and not unknown_runner:
            # Normal stop request only. No waiter, forced stop, or RunPod action.
            try:
                aws('ec2', 'stop-instances', '--instance-ids', ai.INSTANCE)
                report['gpuCleanup'] = 'stop_requested'
            except Exception as error:
                report['gpuCleanup'] = 'stop_request_failed'
                report['gpuCleanupError'] = str(error)
                save()
                raise
        elif started_gpu:
            report['gpuCleanup'] = 'left_running_for_recovery'
        else:
            report['gpuCleanup'] = 'original_state_preserved'
        save()
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
