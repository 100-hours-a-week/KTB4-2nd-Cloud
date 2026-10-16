#!/usr/bin/env python3
"""Receive V2 build evidence and maintain one manifest-only candidate PR."""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('v2_manifest', ROOT / 'scripts/v2-image-manifest.py')
manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest)
CLOUD = '100-hours-a-week/KTB4-2nd-Cloud'
BRANCH = 'automation/v2-release-manifest'
MANIFEST = 'deploy/v2/image-manifest.json'
WORKFLOWS = {'frontend': 'frontend-image.yml', 'backend': 'backend-image.yml', 'ai-app': 'ai-v2-image.yml'}
PAYLOAD_KEYS = {'component', 'source_repository', 'source_sha', 'image', 'digest', 'build_run_id'}


def command(*args, check=True):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, check=check)


def git(*args, check=True):
    return command('git', *args, check=check)


def api(path):
    return json.loads(command('gh', 'api', path).stdout)


def event_payload(event):
    if event.get('action') != 'v2-image-published':
        raise ValueError('Expected v2-image-published; V1 events are not accepted')
    payload = event.get('client_payload', {})
    if not isinstance(payload, dict) or set(payload) != PAYLOAD_KEYS:
        raise ValueError('Expected exact V2 event fields, including build_run_id')
    if not all(isinstance(value, str) for value in payload.values()):
        raise ValueError('V2 event values must be strings')
    if payload['component'] not in WORKFLOWS or not re.fullmatch(r'[1-9][0-9]{0,19}', payload['build_run_id']):
        raise ValueError('Unknown component or invalid build_run_id')
    return payload


def replacement(data, payload):
    updated = copy.deepcopy(data)
    updated['components'][payload['component']] = {
        'sourceRepository': payload['source_repository'], 'sourceSha': payload['source_sha'],
        'image': payload['image'], 'digest': payload['digest'], 'platform': 'linux/amd64',
    }
    return manifest.validate(updated)


def verify_build(payload):
    repository = payload['source_repository']
    run_id = payload['build_run_id']
    for attempt in range(13):
        run = api(f'repos/{repository}/actions/runs/{run_id}')
        if (str(run.get('id')) != run_id or run.get('repository', {}).get('full_name') != repository
                or run.get('head_sha') != payload['source_sha'] or run.get('head_branch') != 'main'
                or run.get('path') != '.github/workflows/' + WORKFLOWS[payload['component']]
                or run.get('event') not in {'push', 'workflow_dispatch'}):
            raise ValueError('Build Run repository/SHA/main/workflow provenance mismatch')
        if run.get('status') == 'completed':
            if run.get('conclusion') != 'success':
                raise ValueError('Image publishing Run did not succeed')
            return
        if attempt < 12:
            # The notification step runs just before the publishing Run completes.
            time.sleep(10)
    raise TimeoutError('Publishing Run not completed; redeliver the event after completion')


def relation(repository, previous, incoming):
    if previous == incoming:
        return 'identical'
    state = api(f'repos/{repository}/compare/{previous}...{incoming}')['status']
    if state not in {'ahead', 'behind', 'identical'}:
        raise ValueError('Diverged source history requires manual review')
    return state


def verify_registry(payload):
    digest = command('docker', 'buildx', 'imagetools', 'inspect', payload['image'],
                     '--format', '{{.Manifest.Digest}}').stdout.strip()
    if digest != payload['digest']:
        raise ValueError('Registry tag and event Digest differ')


def apply_event(path, payload):
    current = manifest.load(path)
    updated = replacement(current, payload)
    state = relation(payload['source_repository'], current['components'][payload['component']]['sourceSha'],
                     payload['source_sha'])
    if state == 'behind':
        return 'stale'
    verify_registry(payload)
    if current == updated:
        return 'unchanged'
    manifest.update(path, argparse.Namespace(**{key: value for key, value in payload.items() if key != 'build_run_id'}))
    return 'updated'


def ensure_pr():
    opened = json.loads(command('gh', 'pr', 'list', '--repo', CLOUD, '--base', 'main', '--head', BRANCH,
                                '--state', 'open', '--json', 'url').stdout)
    if opened:
        return opened[0]['url']
    body = '''## What is this PR? 🔍
V2에서 사용할 FE/BE/AI 이미지 조합을 갱신합니다.

## Changes 📝
- 성공한 main 이미지 발행 Run의 SHA와 Digest 반영
- 다른 서비스의 후보 이미지를 유지하며 조합 갱신

## Test CheckList ✅
- [x] 발행 Run 출처와 Registry Digest 확인
- [ ] Cloud PR Gate의 V2 이미지 통합 검증

## Precaution
PR 병합만으로 배포되지는 않습니다. 스테이징 자동 배포 연결은 후속 작업입니다.

## ✔️ Please check if the PR fulfills these requirements
- [x] main 대상
- [x] hotfix 해당 없음
- [x] yarn lint: N/A, Cloud CI 검증
'''
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md') as file:
        file.write(body)
        file.flush()
        result = command('gh', 'pr', 'create', '--repo', CLOUD, '--base', 'main', '--head', BRANCH,
                         '--title', '[chore] V2 배포 이미지 Manifest 갱신', '--body-file', file.name, check=False)
    if result.returncode == 0:
        return result.stdout.strip()
    # Concurrent publishers may both have observed no PR before one creates it.
    opened = json.loads(command('gh', 'pr', 'list', '--repo', CLOUD, '--base', 'main', '--head', BRANCH,
                                '--state', 'open', '--json', 'url').stdout)
    if not opened:
        raise RuntimeError('Could not create V2 Manifest PR')
    return opened[0]['url']


def publish(payload):
    if git('status', '--porcelain').stdout.strip():
        raise ValueError('Candidate updates require a clean Cloud checkout')
    git('config', 'user.name', 'github-actions[bot]')
    git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    for attempt in range(5):
        git('fetch', 'origin', 'main')
        exists = git('ls-remote', '--exit-code', '--heads', 'origin', BRANCH, check=False)
        if exists.returncode == 0:
            git('fetch', 'origin', BRANCH)
            git('checkout', '-B', BRANCH, 'origin/' + BRANCH)
            changed = git('diff', '--name-only', 'origin/main...HEAD').stdout.splitlines()
            if any(path != MANIFEST for path in changed):
                raise ValueError('Automation branch contains non-manifest changes; manual review required')
            # Conflicts fail closed; never pick either side of a conflicting manifest automatically.
            git('merge', '--no-edit', 'origin/main')
        elif exists.returncode == 2:
            git('checkout', '-B', BRANCH, 'origin/main')
        else:
            raise RuntimeError('Cannot query V2 candidate branch')
        outcome = apply_event(Path(MANIFEST), payload)
        if outcome == 'updated':
            git('add', '--', MANIFEST)
            git('commit', '-m', f"chore: V2 {payload['component']} 배포 이미지 갱신",
                '-m', f"https://github.com/{payload['source_repository']}/actions/runs/{payload['build_run_id']}")
        # This also repairs a push-success/PR-create-failure delivery on replay.
        if not git('diff', '--name-only', 'origin/main', 'HEAD').stdout.strip():
            return {'status': outcome, 'pr': None}
        pushed = git('push', 'origin', BRANCH, check=False)
        if pushed.returncode == 0:
            return {'status': outcome, 'pr': ensure_pr()}
        # No force push. Reload the remote candidate and recheck ancestry after a race.
        if attempt < 4:
            time.sleep((attempt + 1) * 2)
    raise RuntimeError('Candidate branch changed repeatedly or push denied; redeliver the event')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--event-file', type=Path, default=os.environ.get('GITHUB_EVENT_PATH'))
    parser.add_argument('--check-only', action='store_true', help='Validate build and registry; never push or create PRs')
    args = parser.parse_args()
    if not args.event_file:
        raise ValueError('Provide an event file')
    payload = event_payload(json.loads(Path(args.event_file).read_text(), object_pairs_hook=manifest.unique_object))
    # Validate package/SHA before constructing any API path from the event.
    replacement(manifest.load(ROOT / MANIFEST), payload)
    verify_build(payload)
    if args.check_only:
        verify_registry(payload)
        result = {'status': 'validated', 'component': payload['component'], 'buildRunId': payload['build_run_id']}
    else:
        result = publish(payload)
    result.update(component=payload['component'], sourceSha=payload['source_sha'], buildRunId=payload['build_run_id'])
    print(json.dumps(result), flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write('## V2 Manifest event\n\n```json\n' + json.dumps(result) + '\n```\n')


if __name__ == '__main__':
    main()
