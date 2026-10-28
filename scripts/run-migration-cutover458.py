#!/usr/bin/env python3
"""Run #458 on #455 clones, with only explicit cutover overlays."""
import argparse
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import uuid

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT.parent / 'KTB4-2nd-Cloud/.local-docs'
BASE = DOCS / 'evidence/458-sink-shutdown'
OLD = DOCS / 'evidence/455-index-comparison'
CASE = 'index50'
FIXED = {'durationSeconds': 900, 'cutoverSeconds': 360, 'e2eAfterInputSeconds': 120,
         'httpTimeoutSeconds': 100, 'authResponseBudgetSeconds': 55,
         'cdcValidationBudgetSeconds': 45, 'activationBudgetSeconds': 50,
         'sourceApplyConcurrency': 4, 'sourceDrainDeadlineSeconds': 40,
         'snapshotBytesPerSecond': 9000000, 'indexApplied': True,
         'backendCommit': '71bfa648735fbc0d14935149611ad15ea35a45e3',
         'backendImage': 'ghcr.io/100-hours-a-week/yeodam-backend@sha256:d39fd16fc92e77d9c51be9056a2d0c79e355fdf0ad81e38a8d463ab554fd2010'}


def validate_plan(plan):
    if any(plan.get(key) != value for key, value in FIXED.items()):
        raise RuntimeError('Frozen #455 workload/budgets/index differ; no input')
    rates = plan.get('rates', [])
    if len(rates) != 1 or any(rates[0].get(k) != v for k, v in
                             {'rate': 50, 'preAllocatedVUs': 1000, 'maxVUs': 6000}.items()):
        raise RuntimeError('Frozen generator allocation differs; no input')


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def initialize():
    if BASE.exists():
        raise RuntimeError('Preserve the existing attempt; do not overwrite or allocate replacement input')
    manifest = json.loads((OLD / 'bundle-manifest.json').read_text())
    for name, value in manifest['files'].items():
        if hashlib.sha256((OLD / 'runtime/pinned' / name).read_bytes()).hexdigest() != value['renderedSha256']:
            raise RuntimeError('Original #455 artifact differs')
    plan = json.loads((OLD / CASE / 'plan.json').read_text())
    validate_plan(plan)
    expected = ('51474607af2aae8212b32bf4093fcde164dfcbda8f01345e88e8eac595559cb3', 2048038)
    if (plan['commonBaseline']['sha256'], plan['commonBaseline']['trips']) != expected:
        raise RuntimeError('Unexpected historical 50 baseline')
    BASE.mkdir()
    shutil.copytree(OLD / 'runtime/pinned', BASE / 'runtime/pinned')
    overlay = module('overlay458', ROOT / 'scripts/migration-cutover458.py')
    manifest = copy.deepcopy(manifest)
    manifest['issue'] = 458
    manifest['parentManifestSha256'] = hashlib.sha256((OLD / 'bundle-manifest.json').read_bytes()).hexdigest()
    changes = {}
    for file, render in [('b-integration383-cutover.py', overlay.render_boundary),
                         ('b-integration376-control.py', overlay.render_control),
                         ('b-integration386-handoff.py', overlay.render_handoff)]:
        name = 'scripts/' + file; path = BASE / 'runtime/pinned' / name
        before = path.read_text(); after = render(before); compile(after, file, 'exec'); path.write_text(after)
        changes[name] = {'before': hashlib.sha256(before.encode()).hexdigest(), 'after': hashlib.sha256(after.encode()).hexdigest()}
        manifest['files'][name]['renderedSha256'] = changes[name]['after']
    name = 'scripts/migration-cutover458.py'
    shutil.copyfile(ROOT / name, BASE / 'runtime/pinned' / name)
    manifest['files'][name] = {'renderedSha256': hashlib.sha256((ROOT / name).read_bytes()).hexdigest()}
    manifest['overlays'] = changes
    (BASE / 'bundle-manifest.json').write_text(json.dumps(manifest, indent=2))
    plan.update(issue=458, comparisonBaselineRun=plan['rates'][0]['runId'], commonBaseline=None,
                pairedRestoreAttempt=None, cutoverOverlay='Sink cleanup after durable proof; credentials prepared before fence',
                comparisonScope='Same clone baseline/index9MB/50RPS/budgets/BE; cutover control only')
    plan['rates'][0]['runId'] = str(uuid.uuid4())
    directory = BASE / CASE; directory.mkdir()
    (directory / 'plan.json').write_text(json.dumps(plan, indent=2))
    print(json.dumps({'runId': plan['rates'][0]['runId'], 'evidence': str(directory), 'runtime': 'not deployed'}))


def load():
    adapter = module('adapter458', ROOT / 'scripts/run-migration-index455.py')
    adapter.BASE = BASE; adapter.BUNDLE = BASE / 'runtime/pinned'
    return adapter, *adapter.load(CASE)


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('action', choices=['init', 'deploy', 'execute'])
    args = p.parse_args()
    if args.action == 'init': initialize(); return
    adapter, m, success = load()
    directory = BASE / CASE; plan = json.loads((directory / 'plan.json').read_text())
    validate_plan(plan)
    if args.action == 'deploy':
        m.deploy()
        m.raw('kafka', m.ssm.deploy('scripts/migration-cutover458.py', '/run/yeodam-b391/migration-cutover458.py'))
        for host in ('source', 'target'):
            m.raw(host, m.ssm.deploy('scripts/b-integration376-reset.py', '/run/yeodam-b391/b-integration376-reset.py'))
        text = success.render_bulk((adapter.BUNDLE / 'scripts/b-integration376-bulk.py').read_text())
        m.raw('target', ["printf '%s' '" + base64.b64encode(text.encode()).decode() + "' | base64 -d > /run/yeodam-b391/b-integration376-bulk.py"])
        m.raw('source', m.ssm.deploy('scripts/b-integration393-resources.py', '/run/yeodam-b391/b-integration393-resources.py'))
        return
    run = plan['rates'][0]['runId']
    if (directory / (run + '.state.json')).exists(): raise RuntimeError('Run already attempted; preserve evidence')
    receipt = json.loads((directory / 'ready.json').read_text())
    required = ['verified', 'targetBackupReady', 'privatePoolTransferApproved', 'all33TablesAndAutoIncrementEqual', 'indexApplied']
    if receipt.get('runId') != run or receipt.get('hosts') != adapter.HOSTS or not all(receipt.get(k) is True for k in required):
        raise RuntimeError('Exact Run preparation/transfer approval missing')
    if not plan['commonBaseline'] or plan['rates'][0]['rate'] != 50: raise RuntimeError('Only restored 50 RPS is authorized')
    m.execute(plan['rates'][0], 2)  # Same CDC server-id suffix as #455 index50.


if __name__ == '__main__': main()
