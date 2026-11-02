#!/usr/bin/env python3
"""Plan/apply prefix-only AI reference access without replacing unrelated SSO statements."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

ACCOUNT = '483175530259'
REGION = 'ap-northeast-2'
INSTANCE = 'arn:aws:sso:::instance/ssoins-72307cb6e4ae95b6'
PERMISSION_SET = 'arn:aws:sso:::permissionSet/ssoins-72307cb6e4ae95b6/ps-72300040836e1a9e'
NAME = 'YeodamAIWorkerOperator'
ROOT = Path(__file__).resolve().parents[1]
ADDITIONS = json.loads((ROOT / 'deploy/ai-reference/policies.json').read_text())['uploader']['Statement']


def canonical(policy):
    return json.dumps(policy, sort_keys=True, separators=(',', ':'))


def digest(policy):
    return hashlib.sha256(canonical(policy).encode()).hexdigest()


def merge(policy, remove=False):
    result = json.loads(json.dumps(policy))
    statements = result.get('Statement')
    if not isinstance(statements, list):
        raise ValueError('Expected existing statement list; inspect manually')
    for addition in ADDITIONS:
        found = [s for s in statements if s.get('Sid') == addition['Sid']]
        if found and found != [addition]:
            raise ValueError('Existing reference statement differs; preserve and review manually')
        if remove:
            if found:
                statements.remove(found[0])
        elif not found:
            statements.append(json.loads(json.dumps(addition)))
    return result


def apply_policy(aws, before, after, expected_hash, backup_dir):
    if not expected_hash or digest(before) != expected_hash:
        raise RuntimeError('Policy changed or reviewed hash missing; no write performed')
    if not backup_dir:
        raise RuntimeError('--backup-dir is required before apply')
    directory = Path(backup_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f'sso-before-{time.time_ns()}.json'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(before, stream, indent=2)
    current = lambda: json.loads(aws('sso-admin', 'get-inline-policy-for-permission-set',
                                    '--instance-arn', INSTANCE, '--permission-set-arn', PERMISSION_SET)['InlinePolicy'])
    if current() != before:
        raise RuntimeError('Concurrent policy change; no write performed')
    if before != after:
        aws('sso-admin', 'put-inline-policy-to-permission-set', '--instance-arn', INSTANCE,
            '--permission-set-arn', PERMISSION_SET, '--inline-policy', canonical(after))
    request = aws('sso-admin', 'provision-permission-set', '--instance-arn', INSTANCE,
                  '--permission-set-arn', PERMISSION_SET, '--target-type', 'AWS_ACCOUNT',
                  '--target-id', ACCOUNT)['PermissionSetProvisioningStatus']['RequestId']
    print(json.dumps({'provisioningRequestId': request, 'backup': str(path)}), flush=True)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        state = aws('sso-admin', 'describe-permission-set-provisioning-status', '--instance-arn', INSTANCE,
                    '--provision-permission-set-request-id', request)['PermissionSetProvisioningStatus']['Status']
        if state == 'SUCCEEDED':
            if current() != after:
                raise RuntimeError('Policy changed during provisioning; inspect without overwriting')
            print(json.dumps({'provisioned': True, 'policySha256': digest(after), 'requestId': request}), flush=True)
            return
        if state == 'FAILED':
            raise RuntimeError('Provisioning failed; inspect request ' + request)
        time.sleep(3)
    raise RuntimeError('Provisioning pending; inspect request ' + request)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default='yeodam-admin')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--remove', action='store_true', help='Remove only the exact statements owned here')
    parser.add_argument('--expected-policy-sha256')
    parser.add_argument('--backup-dir')
    args = parser.parse_args()

    def aws(*parts):
        process = subprocess.run(['aws', *parts, '--profile', args.profile, '--region', REGION,
                                  '--output', 'json'], capture_output=True, text=True)
        if process.returncode:
            raise RuntimeError('AWS operation failed: ' + ' '.join(parts[:2]))
        return json.loads(process.stdout) if process.stdout.strip() else {}

    if aws('sts', 'get-caller-identity')['Account'] != ACCOUNT:
        raise RuntimeError('Unexpected AWS account')
    name = aws('sso-admin', 'describe-permission-set', '--instance-arn', INSTANCE,
               '--permission-set-arn', PERMISSION_SET)['PermissionSet']['Name']
    if name != NAME:
        raise RuntimeError('Permission set identity differs')
    before = json.loads(aws('sso-admin', 'get-inline-policy-for-permission-set',
                            '--instance-arn', INSTANCE, '--permission-set-arn', PERMISSION_SET)['InlinePolicy'])
    after = merge(before, args.remove)
    print(json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'account': ACCOUNT,
                      'permissionSet': NAME, 'beforeSha256': digest(before), 'afterSha256': digest(after),
                      'changed': before != after, 'remove': args.remove, 'statements': ADDITIONS}), flush=True)
    if args.apply:
        apply_policy(aws, before, after, args.expected_policy_sha256, args.backup_dir)


if __name__ == '__main__':
    main()
