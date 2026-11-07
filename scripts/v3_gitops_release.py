"""Approved FE GitOps bundles. No AWS, kubeconfig, registry credentials or cluster calls.

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
    require(set(policy) == {'schemaVersion', 'target', 'deploymentEnabled', 'branch', 'environment', 'deliveryUnits', 'adoptionReviewed'}, 'Unexpected release policy fields')
    require(type(policy['schemaVersion']) is int and policy['schemaVersion'] == 1, 'Unknown release schema')
    require(policy['target'] == 'yeodam-v3-foundation' and policy['branch'] == BRANCH and policy['environment'] == ENVIRONMENT, 'Only the exact V3 destination is allowed')
    require(policy['deliveryUnits'] == ['frontend'], 'Only FE adoption is implemented; no implicit BE/WS/AI release')
    require(type(policy['deploymentEnabled']) is bool and type(policy['adoptionReviewed']) is bool, 'Explicit boolean switches required')
    require(not policy['deploymentEnabled'] or policy['adoptionReviewed'], 'Existing Helm ownership must be reviewed before activation')
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
    require(candidate.get('candidateVerified') is True and candidate.get('status') == 'disabled', 'Exact verified candidate evidence required')
    commit = candidate['cloudCommit']
    component = candidate['manifest']['components']['frontend']
    image = candidate['images']['frontend']
    require(bool(re.fullmatch(r'ghcr.io/100-hours-a-week/yeodam-frontend@sha256:[a-f0-9]{64}', image)), 'Digest-pinned FE image required')
    require(bool(SHA.fullmatch(component['sourceSha'])), 'FE source SHA required')
    prefix = 'deploy/v3/apps/'
    chart = source_files(commit, prefix)
    files = {'charts/apps/' + n[len(prefix):]: v for n, v in chart.items()}
    overlay = yaml.safe_load(git('show', commit + ':deploy/v3/environments/foundation/frontend.values.yaml'))
    overlay['images'] = {'frontend': image}
    overlay['workloads']['frontend']['enabled'] = True
    overlay['defaults']['configMapData']['RELEASE'] = component['sourceSha']
    overlay['defaults']['configurationRevision'] = digest(canonical(overlay))
    files['values/frontend.json'] = canonical(overlay) + b'\n'
    evidence = {'candidatePr': candidate['pr'], 'cloudCommit': commit, 'cloudGateRunId': candidate['ciRunId'],
                'frontendSource': component['sourceSha'], 'frontendImage': image}
    metadata = {'schemaVersion': 1, 'target': 'yeodam-v3-foundation', 'units': ['frontend'],
                'evidence': evidence, 'files': {n: digest(v) for n, v in sorted(files.items())}}
    metadata['releaseId'] = digest(canonical(metadata))
    files['release.json'] = canonical(metadata) + b'\n'
    validate_bundle(files)
    return files


def validate_bundle(files):
    require('release.json' in files and 'values/frontend.json' in files, 'Incomplete FE bundle')
    require(len(files) < 100 and sum(map(len, files.values())) < 2 * 1024 * 1024, 'Bundle size limit exceeded')
    for name in files:
        path = PurePosixPath(name)
        require(not path.is_absolute() and '..' not in path.parts and str(path) == name, 'Unsafe bundle path')
        require(name in ['release.json', 'values/frontend.json'] or name.startswith('charts/apps/'), 'Only FE chart and values may be published')
    metadata = json.loads(files['release.json'], object_pairs_hook=legacy.checks.manifest.unique_object)
    require(set(metadata) == {'schemaVersion', 'target', 'units', 'evidence', 'files', 'releaseId'}, 'Unexpected bundle metadata')
    require(metadata['schemaVersion'] == 1 and metadata['target'] == 'yeodam-v3-foundation' and metadata['units'] == ['frontend'], 'Wrong bundle destination')
    expected = deepcopy(metadata); expected.pop('releaseId')
    require(metadata['releaseId'] == digest(canonical(expected)), 'Release identity mismatch')
    require(metadata['files'] == {n: digest(v) for n, v in sorted(files.items()) if n != 'release.json'}, 'Bundle file checksum mismatch')
    require('charts/apps/Chart.yaml' in files, 'Pinned Chart snapshot missing')
    image = metadata['evidence']['frontendImage']
    require(bool(re.fullmatch(r'ghcr.io/100-hours-a-week/yeodam-frontend@sha256:[a-f0-9]{64}', image)), 'Wrong FE registry/image')
    with tempfile.TemporaryDirectory(prefix='v3-gitops-render-') as directory:
        root = Path(directory)
        for name, value in files.items():
            path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(value)
        helm = os.environ['V3_HELM']
        source = subprocess.check_output([helm, 'template', 'yeodam-v3-frontend', str(root / 'charts/apps'),
            '--namespace', 'default', '--kube-version', '1.36.0', '-f', str(root / 'values/frontend.json')], text=True, stderr=subprocess.DEVNULL)
        documents = apps.platform.load_documents(source)
        values = apps.merge(yaml.safe_load(files['charts/apps/values.yaml']), json.loads(files['values/frontend.json']))
        apps.validate(documents, values)
        expected_objects = {('Deployment', 'frontend'), ('Service', 'frontend'), ('ConfigMap', 'frontend-runtime')}
        require({(d['kind'], d['metadata']['name']) for d in documents} == expected_objects and len(documents) == 3, 'No platform, Secret or other workload ownership')
        require(all(d['metadata']['namespace'] == 'frontend' for d in documents), 'FE-only namespace boundary')
        deployment = next(d for d in documents if d['kind'] == 'Deployment')
        require(deployment['spec']['template']['spec']['containers'][0]['image'] == image, 'Rendered image differs from approval')
        require(values['defaults']['configMapData']['RELEASE'] == metadata['evidence']['frontendSource'], 'Source and runtime revision differ')
        apps.platform.schema_check(source)
    return metadata


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
        candidate = legacy.prepare(pr_number=pr)
        files = build(candidate)
    metadata = validate_bundle(files)
    return files, {'schemaVersion': 1, 'status': 'prepared' if policy['deploymentEnabled'] else 'disabled',
        'publicationEnabled': policy['deploymentEnabled'], 'published': False, 'deployed': False,
        'releaseId': metadata['releaseId'], 'expectedPrevious': expected_previous,
        'action': 'rollback' if rollback else 'deploy', 'rollbackCommit': rollback,
        'candidatePr': int(pr) if pr else None, 'targetBranch': BRANCH, 'units': ['frontend']}


def verify_approval(context):
    require(context.get('GITHUB_ACTIONS') == 'true' and context.get('GITHUB_REPOSITORY') == CLOUD
            and context.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and context.get('GITHUB_REF') == 'refs/heads/main'
            and context.get('GITHUB_RUN_ATTEMPT') == '1', 'Only first-attempt main manual dispatch may publish; reruns need fresh approval')
    runid = context.get('GITHUB_RUN_ID', '')
    require(bool(re.fullmatch('[1-9][0-9]*', runid)) and bool(SHA.fullmatch(context.get('GITHUB_SHA', ''))), 'Workflow identity missing')
    main = legacy.checks.api(f'repos/{CLOUD}/commits/main')['sha']
    require(main == context['GITHUB_SHA'], 'Workflow main changed since selection')
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
    result = subprocess.run(['gh', 'api', path, '--method', method, '--input', '-'],
        input=json.dumps(payload), text=True, capture_output=True, check=True)
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
    commit = write_api(f'repos/{CLOUD}/git/commits', 'POST', {'message': 'deploy: V3 FE ' + metadata['releaseId'], 'tree': result['sha'],
        'parents': [] if expected_previous == 'absent' else [expected_previous]})
    if expected_previous == 'absent':
        write_api(f'repos/{CLOUD}/git/refs', 'POST', {'ref': 'refs/heads/' + BRANCH, 'sha': commit['sha']})
    else:
        write_api(f'repos/{CLOUD}/git/refs/heads/{BRANCH}', 'PATCH', {'sha': commit['sha'], 'force': False})
    return report | {'status': 'published', 'published': True, 'approvedCommit': commit['sha'], 'deployed': False}
