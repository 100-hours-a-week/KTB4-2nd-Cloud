#!/usr/bin/env python3
"""Offline plan/render by default. Live read/execute require explicit subcommands.

Create the mode-700 output directory yourself. No command installs Helm releases,
reads Secret resources, changes offsets, restarts tasks, or creates trial jobs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import v3_cdc_fault as f


def load(p):
    f.require(not p.is_symlink() and p.stat().st_size <= 4 * 1024 * 1024, 'Invalid input file')
    return json.loads(p.read_text())


def render(s, output):
    f.validate_snapshot(s)
    lock = load(f.CHART / 'upstream.lock.json')
    archive = f.CHART / ('chaos-mesh-' + lock['version'] + '.tgz')
    f.require(hashlib.sha256(archive.read_bytes()).hexdigest() == lock['sha256'], 'Upstream chart checksum mismatch')
    import v3_chaos_install as install
    values = install.values(s)
    f.private_write(output, values)
    return {'installValuesPrepared': True, 'installed': False, 'newNodes': 0,
            'controllerPods': 1, 'daemonNodes': [s['connect']['node']],
            'requests': {'cpu': '150m', 'memory': '192Mi'},
            'warning': 'Privileged hostPID daemon and upstream cluster CRDs/RBAC require installation review'}


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    a = sub.add_parser('snapshot', help='Read-only target identities; no secrets or installation')
    a.add_argument('--kubeconfig', required=True, type=Path); a.add_argument('--output', required=True, type=Path)
    a.add_argument('--controller-node', help='Explicit existing hostname; no automatic app-node fallback')
    a.add_argument('--allow-worker-controller', action='store_true', help='Reviewed temporary CPU worker placement only')
    for name in ('plan', 'install-values', 'render-install'):
        a = sub.add_parser(name)
        a.add_argument('--snapshot', required=True, type=Path); a.add_argument('--output', required=True, type=Path)
        if name == 'plan':
            a.add_argument('--run-id', required=True); a.add_argument('--seconds', required=True, type=int)
    a = sub.add_parser('collect', help='Read trial metadata using credentials ONLY inside MySQL pod')
    a.add_argument('--kubeconfig', required=True, type=Path); a.add_argument('--trip-id', required=True, type=int, action='append')
    a.add_argument('--ack-trial-metadata-read', action='store_true', required=True)
    a.add_argument('--output', required=True, type=Path)
    a = sub.add_parser('compare', help='Offline comparison, never invent missing evidence')
    a.add_argument('--before', required=True, type=Path); a.add_argument('--after', required=True, type=Path)
    a.add_argument('--output', required=True, type=Path)
    for name in ('run', 'recover'):
        a = sub.add_parser(name)
        a.add_argument('--kubeconfig', required=True, type=Path); a.add_argument('--plan', required=True, type=Path)
        a.add_argument('--output', required=True, type=Path)
        if name == 'run':
            a.add_argument('--approve-plan-sha256', required=True)
            a.add_argument('--ack-shared-cdc-maintenance', action='store_true', required=True)
        else:
            a.add_argument('--receipt', required=True, type=Path)
    a = p.parse_args()
    # Refuse existing output before opening a cluster session or mutating anything.
    f.require(not a.output.exists() and not a.output.is_symlink() and a.output.parent.is_dir()
              and a.output.parent.stat().st_mode & 0o077 == 0, 'Use a fresh output under a mode-700 directory')
    if a.action == 'snapshot':
        result = f.snapshot(f.Kube(a.kubeconfig), a.controller_node, a.allow_worker_controller); f.private_write(a.output, result)
        print(json.dumps({'snapshotPrepared': True, 'mutation': False})); return
    if a.action == 'plan':
        result = f.prepare(load(a.snapshot), a.run_id, a.seconds); f.private_write(a.output, result)
        print(json.dumps({'planSha256': result['planSha256'], 'scope': result['scope'], 'paused': True})); return
    if a.action == 'install-values':
        print(json.dumps(render(load(a.snapshot), a.output))); return
    if a.action == 'render-install':
        import v3_chaos_install as install
        s = load(a.snapshot)
        with tempfile.TemporaryDirectory(prefix='v3-chaos-render-') as directory:
            value_file = Path(directory) / 'values.json'
            f.private_write(value_file, install.values(s))
            docs = install.render(s, os.environ.get('V3_HELM', 'helm'), value_file)
        docs.insert(0, {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {
            'name': 'chaos-mesh', 'labels': {'app.kubernetes.io/part-of': 'yeodam',
                                           'pod-security.kubernetes.io/enforce': 'privileged'}}})
        f.private_write(a.output, {'apiVersion': 'v1', 'kind': 'List', 'items': docs})
        print(json.dumps({'preparedResources': len(docs), 'installed': False,
                          'warning': 'Fresh local TLS keys in this private bundle; never upload to CI/Git/chat'})); return
    if a.action == 'collect':
        result = f.collect_db(f.Kube(a.kubeconfig), a.trip_id)
    elif a.action == 'compare':
        result = f.compare_evidence(load(a.before), load(a.after))
    elif a.action == 'run':
        result = f.execute(f.Kube(a.kubeconfig), load(a.plan), a.approve_plan_sha256, a.output)
        print(json.dumps({'runFinished': True, 'injectionConfirmed': result['injectionConfirmed'],
                          'businessRecoveryVerified': False}))
        if not result['injectionConfirmed']:
            sys.exit(2)
        return
    else:
        plan = load(a.plan); receipt = load(a.receipt)
        # Recovery remains available after snapshot expiration or a code upgrade.
        f.require(receipt['planSha256'] == plan['planSha256'] == f.sha({k:v for k,v in plan.items() if k != 'planSha256'}), 'Recovery plan/receipt mismatch')
        k = f.Kube(a.kubeconfig)
        f.require(k.identity() == plan['snapshot']['kubeSystemUid'], 'Wrong recovery cluster')
        result = f.recover(k, plan, receipt['faultUid'])
    f.private_write(a.output, result)
    print(json.dumps({'evidenceSaved': True, 'verdict': result.get('verdict', 'not_assessed')}))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError) as e:
        # Do not forward kubectl/SQL exception bodies, stdout, or private input.
        reason = str(e) if isinstance(e, f.GuardError) else 'Inspect the documented prerequisites; raw diagnostics withheld'
        print(json.dumps({'status': 'blocked', 'errorType': type(e).__name__, 'details': reason}), file=sys.stderr)
        sys.exit(1)
