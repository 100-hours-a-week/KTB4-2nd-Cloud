#!/usr/bin/env python3
"""Replay the pinned successful B flow on the #444 clones.

Only endpoint identities, private backup namespaces and a k6 DNS override differ.
Workload/consumer/cutover behavior remains the 2a2907f + 9MB/s success flow.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT.parent / 'KTB4-2nd-Cloud/.local-docs/evidence/455-index-comparison'
PIN = '2a2907fab56e2a9f61d3faf6e069a79a38f76e01'
BUNDLE = BASE / 'runtime/pinned'
HOSTS = {'source': 'i-03efe8652117623c2', 'target': 'i-0c9aee73a0dc9b59d',
         'kafka': 'i-0b86e517fda2cfafc', 'generator': 'i-0c53b65eb35dd2b42'}
REPLACE = {'i-05d320e3a87200d5a': HOSTS['source'], 'i-05535ae6ff812972c': HOSTS['target'],
           '10.30.0.174': '10.30.0.245', '10.30.10.28': '10.30.10.45',
           'migration/rehearsal-reset/': 'migration/performance455/rehearsal-reset/',
           'migration/v1-source/': 'migration/performance455/v1-source/'}


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def build():
    if BUNDLE.exists():
        raise RuntimeError('Bundle already exists; verify its manifest instead of overwriting')
    files = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', PIN, 'scripts', 'tests/load'], cwd=ROOT, text=True).splitlines()
    files = [x for x in files if Path(x).suffix in ('.py', '.sh', '.js', '.mjs', '.json')]
    files.append('terraform/v2/staging/files/capture-mysql-replication-source.sh')
    changes = {}
    for name in files:
        raw = subprocess.check_output(['git', 'show', PIN + ':' + name], cwd=ROOT)
        text = raw.decode()
        for old, new in REPLACE.items():text = text.replace(old, new)
        if name.endswith('run-synthetic-pool-ssm.py'):
            text = text.replace('yeodam-v2-staging-v1-source-app', 'yeodam-migration-perf-source')
        if name.endswith('scenarios/migration-b-integration.js'):
            needle = 'export const options = {'
            assert text.count(needle) == 1
            text = text.replace(needle, needle + "\n  hosts: {'v1-staging.yeodam-2gether.com': '10.30.0.245'},", 1)
        if name.endswith('b2-pilot370-host.py'):
            start = text.index('def pull_image():')
            end = text.find('\ndef ', start + 5)
            if end < 0:end = text.index("\nif __name__", start)
            text = text[:start] + "def pull_image():\n    from importlib.machinery import SourceFileLoader\n    SourceFileLoader('prep444', '/var/lib/yeodam/migration444/prepare.py').load_module().ensure_image()\n\n" + text[end:]
        path = BUNDLE / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        changes[name] = {'originalSha256': hashlib.sha256(raw).hexdigest(), 'renderedSha256': hashlib.sha256(text.encode()).hexdigest()}
    observer = 'scripts/b-integration393-resources.py'
    raw = subprocess.check_output(['git', 'show', '4d30be2:' + observer], cwd=ROOT)
    text = raw.decode()
    for old, new in REPLACE.items():text = text.replace(old, new)
    (BUNDLE / observer).write_text(text)
    changes[observer] = {'originalSha256': hashlib.sha256(raw).hexdigest(), 'renderedSha256': hashlib.sha256(text.encode()).hexdigest(), 'observerPin': '4d30be2'}
    # Public bridge source is unchanged from the successful auth implementation.
    manifest = {'cloudPin': PIN, 'endpointMap': REPLACE, 'files': changes,
                'k6Change': 'hosts option only; same URL, SNI, HTTP workload and timeouts',
                'imagePullChange': 'cached pinned digest / existing staging GHCR Secret; no workload change'}
    (BASE / 'bundle-manifest.json').write_text(json.dumps(manifest, indent=2))
    contract = json.loads((ROOT / 'tests/load/fixtures/migration-index455-contract.json').read_text())
    for case in contract['cases']:
        directory = BASE / case['name'];directory.mkdir()
        historical = json.loads((BASE.parent / '408-snapshot-contention/historic391-limited-30/plan.json').read_text())
        case_run = str(uuid.uuid4())
        historical.update(issue=455, rates=[{'rate': case['rate'], 'runId': case_run,
                         'preAllocatedVUs': case['preAllocatedVUs'], 'maxVUs': case['maxVUs']}],
                         comparisonBaselineRun=None, indexApplied=case['index'], cloneHosts=HOSTS,
                         pairedRestoreAttempt=case['name'], comparisonScope='Latest successful9MB/s B flow, index only; clone endpoint identities mapped')
        historical['commonBaseline'] = None # Set from actual verified restoration, never a historical receipt.
        (directory / 'plan.json').write_text(json.dumps(historical, indent=2))
    print(json.dumps({'bundleFiles': len(files), 'cases': [c['name'] for c in contract['cases']]}))


def load(case):
    manifest = json.loads((BASE / 'bundle-manifest.json').read_text())
    for name, hashes in manifest['files'].items():
        if hashlib.sha256((BUNDLE / name).read_bytes()).hexdigest() != hashes['renderedSha256']:
            raise RuntimeError('Rendered artifact changed: ' + name)
    m = module('runner455', BUNDLE / 'scripts/b-integration376-run.py')
    evidence = BASE / case
    m.EVIDENCE = m.ssm.EVIDENCE = evidence
    m.ssm.HOSTS = dict(HOSTS)
    success = module('success455', ROOT / 'scripts/run-b408-success-version.py')
    success.LEGACY = BUNDLE;success.EVIDENCE = evidence
    # Only the snapshot template's S3 namespace is adapted by the rendered bundle.
    success.instrument(m, {**json.loads((evidence / 'plan.json').read_text())['rates'][0], 'snapshotBytesPerSecond': 9000000})
    # Keep the original E2E service lifecycle, overriding only its cloned DB URL.
    original_aws = m.ssm.aws
    def aws(*args):
        if args[:2] == ('ecs', 'register-task-definition') and '--cli-input-json' in args:
            path = Path(args[args.index('--cli-input-json') + 1].removeprefix('file://'))
            candidate = json.loads(path.read_text())
            for c in candidate['containerDefinitions']:
                for env in c.get('environment', []):
                    if env['name'] == 'MYSQL_URL':env['value'] = env['value'].replace('10.30.10.28', '10.30.10.45')
            path.write_text(json.dumps(candidate))
        return original_aws(*args)
    m.ssm.aws = aws
    return m, success


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['build', 'deploy', 'execute'])
    p.add_argument('--case', choices=['control30', 'index30', 'index50'])
    a = p.parse_args()
    if a.action == 'build':build();return
    if not a.case:raise ValueError('Case required')
    m, success = load(a.case)
    plan = json.loads((BASE / a.case / 'plan.json').read_text())
    if a.action == 'deploy':
        m.deploy()
        for host in ('source', 'target'):
            m.raw(host, m.ssm.deploy('scripts/b-integration376-reset.py', '/run/yeodam-b391/b-integration376-reset.py'))
        data = success.render_bulk((BUNDLE / 'scripts/b-integration376-bulk.py').read_text())
        import base64
        m.raw('target', ["printf '%s' '" + base64.b64encode(data.encode()).decode() + "' | base64 -d > /run/yeodam-b391/b-integration376-bulk.py"])
        m.raw('source', m.ssm.deploy('scripts/b-integration393-resources.py', '/run/yeodam-b391/b-integration393-resources.py'))
    else:
        if (BASE / a.case / (plan['rates'][0]['runId'] + '.state.json')).exists():
            raise RuntimeError('Run already attempted; preserve evidence and use a new approved Run ID')
        receipt = BASE / a.case / 'ready.json'
        if not receipt.exists():raise RuntimeError('No verified per-case preparation receipt; no load')
        value = json.loads(receipt.read_text())
        if value.get('runId') != plan['rates'][0]['runId'] or value.get('hosts') != HOSTS or not value.get('verified'):
            raise RuntimeError('Case preparation receipt does not match this clone run')
        if value.get('targetBackupReady') is not True or not plan.get('commonBaseline'):
            raise RuntimeError('Verify the clone backup command and record the fresh common baseline before input')
        if value.get('privatePoolTransferApproved') is not True:
            raise RuntimeError('This exact Run needs approval for its private synthetic pool transfer')
        m.execute(plan['rates'][0], ['control30','index30','index50'].index(a.case))

if __name__ == '__main__':main()
