#!/usr/bin/env python3
"""Verify a merged Cloud image candidate; never deploy ECS or Kubernetes."""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('release_checks', ROOT / 'scripts/deploy-v2-staging-release.py')
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)
POLICY = ROOT / 'deploy/production/release-policy.json'
PUBLISHERS = {'frontend': 'frontend-image.yml', 'backend': 'backend-image.yml', 'ai-app': 'ai-v2-image.yml'}


def read_policy():
    policy = json.loads(POLICY.read_text(), object_pairs_hook=checks.manifest.unique_object)
    if (not isinstance(policy, dict) or set(policy) != {'schemaVersion', 'target', 'deploymentEnabled'}
            or type(policy['schemaVersion']) is not int or policy['schemaVersion'] != 1
            or type(policy['deploymentEnabled']) is not bool):
        raise ValueError('Invalid production release policy')
    if policy['target'] != 'kubernetes':
        raise ValueError('Only the future Kubernetes production target is allowed; ECS fallback is forbidden')
    if policy['deploymentEnabled']:
        raise ValueError('Kubernetes deployment executor is not connected; keep deploymentEnabled=false')
    return policy


def verify_publishers(data):
    # Cloud candidate creation already checks the incoming publisher Run and GHCR
    # digest. Recheck successful main publishing evidence for every chosen image.
    for name, entry in data['components'].items():
        repository, sha = entry['sourceRepository'], entry['sourceSha']
        workflow = PUBLISHERS[name]
        runs = checks.api(f'repos/{repository}/actions/workflows/{workflow}/runs?head_sha={sha}&branch=main&per_page=100')['workflow_runs']
        matching = [r for r in runs if r.get('head_sha') == sha and r.get('head_branch') == 'main'
                    and r.get('path') == '.github/workflows/' + workflow
                    and r.get('event') in {'push', 'workflow_dispatch'}
                    and (r.get('repository') or {}).get('full_name') == repository
                    and (r.get('head_repository') or {}).get('full_name') == repository]
        if not matching:
            raise ValueError('No matching main image publisher: ' + name)
        latest = max(matching, key=lambda r: r['id'])
        if latest.get('status') != 'completed' or latest.get('conclusion') != 'success':
            raise ValueError('Latest matching image publisher did not succeed: ' + name)


def prepare(pr_number=None, commit=None, apply=False):
    policy = read_policy()
    # No mutation credentials, AWS commands or deploy executable exist here.
    if apply:
        raise ValueError('Production deployment is disabled; this command only prepares evidence')
    if (pr_number is None) == (commit is None):
        raise ValueError('Select exactly one merged Cloud PR or merge commit')
    if commit is not None:
        pr_number = checks.merged_pr_for_commit(commit)
    if not re.fullmatch('[1-9][0-9]*', str(pr_number)):
        raise ValueError('A merged Cloud PR number is required')
    pr = checks.api(f'repos/{checks.CLOUD}/pulls/{pr_number}')
    if (not pr.get('merged') or pr['base']['ref'] != 'main'
            or pr['base']['repo']['full_name'] != checks.CLOUD
            or (pr['head'].get('repo') or {}).get('full_name') != checks.CLOUD
            or not re.fullmatch('[0-9a-f]{40}', str(pr.get('merge_commit_sha', '')))
            or (commit is not None and pr['merge_commit_sha'] != commit)):
        raise ValueError('Only an exact merged Cloud-to-main candidate is accepted')
    # Policy/code PRs cannot masquerade as a release approval, even if their
    # resulting manifest happens to equal the latest manifest on main.
    files = checks.api(f'repos/{checks.CLOUD}/pulls/{pr_number}/files?per_page=100')
    if (pr.get('changed_files') != 1 or len(files) != 1
            or files[0].get('filename') != checks.MANIFEST or files[0].get('status') != 'modified'):
        raise ValueError('Release approval must change only the image manifest')
    selected = checks.candidate(str(pr_number))
    report = {'schemaVersion': 1, 'target': policy['target'], 'deploymentEnabled': False,
              'executorReady': False, 'deployed': False,
              'checkedAt': datetime.now(timezone.utc).isoformat()}
    if selected is None:
        return report | {'status': 'superseded', 'pr': int(pr_number),
                         'reason': 'A newer main image manifest supersedes this candidate'}
    verify_publishers(selected['manifest'])
    return report | selected | {'status': 'disabled', 'candidateVerified': True,
                                 'images': checks.image_refs(selected['manifest']),
                                 'reason': 'Candidate verified; production deployment remains disabled'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--pr')
    source.add_argument('--commit')
    parser.add_argument('--apply', action='store_true', help='Always rejected until a reviewed executor is implemented')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare(args.pr, args.commit, args.apply)
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        reason = 'GitHub evidence lookup failed; no deployment attempted' if isinstance(error, subprocess.CalledProcessError) else str(error)
        result = {'schemaVersion': 1, 'status': 'rejected', 'deployed': False, 'reason': reason}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))
        return 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
