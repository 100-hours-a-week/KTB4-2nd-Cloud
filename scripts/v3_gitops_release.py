"""Approved full-app GitOps bundles. No AWS, kubeconfig, registry credentials or cluster calls.

Publication defaults off. An enabled policy still needs a first-attempt main-only
workflow_dispatch, protected Environment approval, adoption review, unchanged main,
unchanged approved ref and a byte-identical preview before any Git write.
"""
import base64
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import yaml

ROOT = Path(__file__).resolve().parents[1]
CLOUD = '100-hours-a-week/KTB4-2nd-Cloud'
BRANCH = 'deploy/v3-approved'
ENVIRONMENT = 'v3-production'
POLICY = ROOT / 'deploy/v3/releases/policy.json'
import v3_release_bundle as bundle
from v3_delivery_contract import UNITS, CHARTS
from v3_release_candidate import verify_merged
SHA = re.compile(r'[a-f0-9]{40}')


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legacy = load_script('prepare-production-release')
apps = load_script('check-v3-apps')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_policy():
    policy = json.loads(POLICY.read_text(), object_pairs_hook=legacy.checks.manifest.unique_object)
    require(set(policy) == {'schemaVersion', 'target', 'deploymentEnabled', 'branch', 'environment', 'deliveryUnits', 'adoptionReviewed', 'observerInstanceId'}, 'Unexpected release policy fields')
    require(type(policy['schemaVersion']) is int and policy['schemaVersion'] == 2, 'Unknown release schema')
    require(policy['target'] == 'yeodam-v3-foundation' and policy['branch'] == BRANCH and policy['environment'] == ENVIRONMENT, 'Only the exact V3 destination is allowed')
    require(policy['deliveryUnits'] == UNITS, 'Exactly all nine reviewed app roles required')
    require(type(policy['deploymentEnabled']) is bool and type(policy['adoptionReviewed']) is bool, 'Explicit boolean switches required')
    require(not policy['deploymentEnabled'] or policy['adoptionReviewed'], 'Existing Helm ownership must be reviewed before activation')
    require(isinstance(policy['observerInstanceId'],str) and (not policy['observerInstanceId'] or re.fullmatch('i-[a-f0-9]{17}',policy['observerInstanceId'])), 'Exact observer system instance required')
    require(not policy['deploymentEnabled'] or bool(policy['observerInstanceId']), 'Read-only delivery observation must be configured before activation')
    return policy


def git(*args, binary=False):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=not binary, stderr=subprocess.DEVNULL)


def source_files(commit, prefix):
    require(bool(SHA.fullmatch(commit)), 'Exact source commit required')
    items = git('ls-tree', '-rz', commit, *(['--', prefix] if prefix else []), binary=True).split(b'\0')
    result = {}
    for item in filter(None, items):
        meta, path = item.split(b'\t', 1)
        mode, kind, _ = meta.split()
        name = path.decode()
        require(mode in [b'100644', b'100755'] and kind == b'blob', 'No symlink or submodule sources')
        data = git('show', commit + ':' + name, binary=True)
        require(len(data) < 1024 * 1024, 'Oversized bundle file')
        result[name] = data
    require(bool(result), 'Source files missing at candidate commit')
    return result


def build(candidate):
    require(candidate.get('candidateVerified') is True and candidate.get('status') == 'verified', 'Exact verified V3 candidate required')
    files = {}
    for chart in CHARTS:
        prefix = 'deploy/v3/' + chart + '/'
        files.update({'charts/' + chart + '/' + n[len(prefix):]: v for n, v in source_files(candidate['cloudCommit'], prefix).items()})
    result = bundle.build(candidate['candidate'], files, {'candidatePr': candidate['pr'], 'cloudCommit': candidate['cloudCommit'], 'cloudGateRunId': candidate['ciRunId']})
    validate_bundle(result)
    return result


def validate_bundle(files):
    return bundle.validate(files)


def rollback_bundle(commit, current):
    require(bool(SHA.fullmatch(commit)) and bool(SHA.fullmatch(current)) and commit != current, 'Select a distinct exact previous release')
    git('merge-base', '--is-ancestor', commit, current)
    files = source_files(commit, '')
    validate_bundle(files)
    return files


def current_ref():
    # matching-refs allows absence without suppressing unrelated authentication errors.
    refs = legacy.checks.api(f'repos/{CLOUD}/git/matching-refs/heads/{BRANCH}')
    matches = [x for x in refs if x['ref'] == 'refs/heads/' + BRANCH]
    require(len(matches) <= 1, 'Ambiguous approved branch')
    return matches[0]['object']['sha'] if matches else 'absent'


def prepare(pr, rollback, expected_previous):
    policy = read_policy()
    require(expected_previous == 'absent' or bool(SHA.fullmatch(expected_previous)), 'Exact expected approved commit or absent required')
    require(current_ref() == expected_previous, 'Approved branch changed; select and approve a fresh request')
    require((pr is None) != (rollback is None), 'Select candidate PR or rollback commit')
    if rollback:
        files = rollback_bundle(rollback, expected_previous)
    else:
        candidate = verify_merged(pr, legacy.checks.api)
        files = build(candidate)
    metadata = validate_bundle(files)
    return files, {'schemaVersion': 2, 'status': 'prepared' if policy['deploymentEnabled'] else 'disabled',
        'publicationEnabled': policy['deploymentEnabled'], 'published': False, 'deployed': False,
        'releaseId': metadata['releaseId'], 'expectedPrevious': expected_previous,
        'action': 'rollback' if rollback else 'deploy', 'rollbackCommit': rollback,
        'candidatePr': int(pr) if pr else None, 'targetBranch': BRANCH, 'units': UNITS}


def verify_approval(context):
    require(context.get('GITHUB_ACTIONS') == 'true' and context.get('GITHUB_REPOSITORY') == CLOUD
            and context.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and context.get('GITHUB_REF') == 'refs/heads/main'
            and context.get('GITHUB_RUN_ATTEMPT') == '1' and context.get('GITHUB_JOB') == 'publish', 'Only first-attempt main manual dispatch may publish; reruns need fresh approval')
    runid = context.get('GITHUB_RUN_ID', '')
    require(bool(re.fullmatch('[1-9][0-9]*', runid)) and bool(SHA.fullmatch(context.get('GITHUB_SHA', ''))), 'Workflow identity missing')
    main = legacy.checks.api(f'repos/{CLOUD}/commits/main')['sha']
    require(main == context['GITHUB_SHA'], 'Workflow main changed since selection')
    run = legacy.checks.api(f'repos/{CLOUD}/actions/runs/{runid}')
    require(run.get('event') == 'workflow_dispatch' and run.get('head_branch') == 'main'
            and run.get('head_sha') == context['GITHUB_SHA'] and run.get('run_attempt') == 1
            and run.get('path') == '.github/workflows/v3-approved-release.yml'
            and run.get('status') in {'in_progress', 'waiting'}
            and (run.get('head_repository') or {}).get('full_name') == CLOUD, 'Approval must belong to this active release workflow')
    # The aggregate Run can remain waiting after its protected Job starts.
    # Bind publication to the actual active Job in this exact first attempt.
    jobs = legacy.checks.api(f'repos/{CLOUD}/actions/runs/{runid}/attempts/1/jobs?per_page=100')
    publishers = [job for job in jobs.get('jobs', []) if job.get('name') == 'publish']
    require(len(publishers) == 1 and publishers[0].get('status') == 'in_progress'
            and publishers[0].get('run_id') == int(runid)
            and publishers[0].get('head_sha') == context['GITHUB_SHA'],
            'Publication requires this attempt active publish Job')
    environment = legacy.checks.api(f'repos/{CLOUD}/environments/{ENVIRONMENT}')
    require(any(r['type'] == 'required_reviewers' and r.get('reviewers') for r in environment.get('protection_rules', [])), 'Environment needs actual required reviewers')
    require(environment.get('deployment_branch_policy') == {'protected_branches': False, 'custom_branch_policies': True}, 'Environment must restrict deployment to main')
    branches = legacy.checks.api(f'repos/{CLOUD}/environments/{ENVIRONMENT}/deployment-branch-policies')['branch_policies']
    require(len(branches) == 1 and branches[0]['name'] == 'main' and branches[0]['type'] == 'branch', 'Only main is allowed in deployment Environment')
    rules = legacy.checks.api(f'repos/{CLOUD}/rules/branches/deploy%2Fv3-approved')
    require({'update', 'deletion', 'non_fast_forward'} <= {r['type'] for r in rules}, 'Approved ref needs restricted updates and deletion/force-push protection')
    reviews = legacy.checks.api(f'repos/{CLOUD}/actions/runs/{runid}/approvals')
    selected = [r for r in reviews if any(e['id'] == environment['id'] and e['name'] == ENVIRONMENT for e in r.get('environments', []))]
    require(bool(selected) and all(r['state'] == 'approved' for r in selected), 'No unambiguous approval for this run and Environment')


def write_api(path, method, payload):
    require(path.startswith(f'repos/{CLOUD}/git/'), 'Only approved repository Git objects may be written')
    token = os.environ.get('V3_GIT_WRITE_TOKEN')
    require(bool(token), 'Approved Cloud release writer token is required; no fallback to the Actions token')
    result = subprocess.run(['gh', 'api', path, '--method', method, '--input', '-'],
        input=json.dumps(payload), text=True, capture_output=True, check=True,
        env=dict(os.environ, GH_TOKEN=token))
    return json.loads(result.stdout)


def publish(pr, rollback, expected_previous, approved_release_id, context=None):
    policy = read_policy()
    require(policy['deploymentEnabled'] and policy['adoptionReviewed'], 'Publication disabled; no Git or cluster mutation')
    verify_approval(context or os.environ)
    # Rebuild after approval rather than trusting a mutable branch or downloaded artifact.
    files, report = prepare(pr, rollback, expected_previous)
    require(report['releaseId'] == approved_release_id, 'Candidate/chart/config changed after preview')
    metadata = validate_bundle(files)
    # Recheck immediately before creating Git objects. Final non-force ref update is CAS-safe.
    require(current_ref() == expected_previous, 'Approved ref moved after preview')
    require(legacy.checks.api(f'repos/{CLOUD}/commits/main')['sha'] == (context or os.environ)['GITHUB_SHA'], 'Workflow main moved during validation')
    tree = []
    for name, value in sorted(files.items()):
        blob = write_api(f'repos/{CLOUD}/git/blobs', 'POST', {'content': base64.b64encode(value).decode(), 'encoding': 'base64'})
        tree.append({'path': name, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
    result = write_api(f'repos/{CLOUD}/git/trees', 'POST', {'tree': tree})
    commit = write_api(f'repos/{CLOUD}/git/commits', 'POST', {'message': 'deploy: V3 apps ' + metadata['releaseId'], 'tree': result['sha'],
        'parents': [] if expected_previous == 'absent' else [expected_previous]})
    if expected_previous == 'absent':
        write_api(f'repos/{CLOUD}/git/refs', 'POST', {'ref': 'refs/heads/' + BRANCH, 'sha': commit['sha']})
    else:
        write_api(f'repos/{CLOUD}/git/refs/heads/{BRANCH}', 'PATCH', {'sha': commit['sha'], 'force': False})
    return report | {'status': 'published', 'published': True, 'approvedCommit': commit['sha'], 'deployed': False}
