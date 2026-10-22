#!/usr/bin/env python3
"""Add the requested staging GPU SSM Shell target to the existing AI permission set."""
import argparse
import hashlib
import json
import subprocess
import time

ACCOUNT = '483175530259'
REGION = 'ap-northeast-2'
INSTANCE = 'arn:aws:sso:::instance/ssoins-72307cb6e4ae95b6'
PERMISSION_SET = 'arn:aws:sso:::permissionSet/ssoins-72307cb6e4ae95b6/ps-72300040836e1a9e'
TARGET = 'i-05e1ad9dd96dba4ae'
TARGET_ARN = f'arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{TARGET}'
SID = 'StartAIStagingQdrantShellSession'
SHELL_DOCUMENT = f'arn:aws:ssm:{REGION}:{ACCOUNT}:document/SSM-SessionManagerRunShell'


def canonical(policy):
    return json.dumps(policy, sort_keys=True, separators=(',', ':'))


def digest(policy):
    return hashlib.sha256(canonical(policy).encode()).hexdigest()


def extend(policy):
    result = json.loads(json.dumps(policy))
    statement = {'Sid': SID, 'Effect': 'Allow', 'Action': 'ssm:StartSession',
                 'Resource': [TARGET_ARN, SHELL_DOCUMENT]}
    found = [s for s in result['Statement'] if s.get('Sid') == SID]
    if found and found != [statement]:
        raise ValueError('Existing statement differs; preserve it and review manually')
    if not found:
        result['Statement'].append(statement)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default='yeodam-admin')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-policy-sha256')
    args = parser.parse_args()

    def aws(*parts):
        process = subprocess.run(['aws', *parts, '--profile', args.profile, '--region', REGION,
                                  '--output', 'json'], capture_output=True, text=True)
        if process.returncode:
            raise RuntimeError('AWS operation failed: ' + ' '.join(parts[:2]))
        return json.loads(process.stdout) if process.stdout.strip() else {}

    def current():
        return json.loads(aws('sso-admin', 'get-inline-policy-for-permission-set',
                              '--instance-arn', INSTANCE, '--permission-set-arn', PERMISSION_SET)['InlinePolicy'])

    if aws('sts', 'get-caller-identity')['Account'] != ACCOUNT:
        raise RuntimeError('Unexpected AWS account')
    named = aws('sso-admin', 'describe-permission-set', '--instance-arn', INSTANCE,
                '--permission-set-arn', PERMISSION_SET)['PermissionSet']['Name']
    if named != 'YeodamAIWorkerOperator':
        raise RuntimeError('Permission set identity differs')
    before = current()
    after = extend(before)
    print(json.dumps({'account': ACCOUNT, 'permissionSet': named, 'beforeSha256': digest(before),
                      'afterSha256': digest(after), 'changed': before != after,
                      'addedTarget': TARGET, 'action': 'ssm:StartSession',
                      'newStartStopOrSendCommandPermissions': False}), flush=True)
    if not args.apply:
        return
    if not args.expected_policy_sha256 or digest(before) != args.expected_policy_sha256:
        raise RuntimeError('Policy changed or no reviewed hash supplied; no write performed')
    host = aws('ec2', 'describe-instances', '--instance-ids', TARGET)['Reservations'][0]['Instances'][0]
    tags = {t['Key']: t['Value'] for t in host.get('Tags', [])}
    if tags.get('Name') != 'yeodam-v2-staging-ai-gpu' or tags.get('Environment') != 'staging':
        raise RuntimeError('Target is not the reviewed staging GPU host')
    if current() != before:
        raise RuntimeError('Concurrent policy change; no write performed')
    if before != after:
        aws('sso-admin', 'put-inline-policy-to-permission-set', '--instance-arn', INSTANCE,
            '--permission-set-arn', PERMISSION_SET, '--inline-policy', canonical(after))
    request = aws('sso-admin', 'provision-permission-set', '--instance-arn', INSTANCE,
                  '--permission-set-arn', PERMISSION_SET, '--target-type', 'AWS_ACCOUNT',
                  '--target-id', ACCOUNT)['PermissionSetProvisioningStatus']['RequestId']
    print(json.dumps({'provisioningRequestId': request}), flush=True)
    end = time.monotonic() + 120
    while time.monotonic() < end:
        result = aws('sso-admin', 'describe-permission-set-provisioning-status',
                     '--instance-arn', INSTANCE, '--provision-permission-set-request-id', request)
        status = result['PermissionSetProvisioningStatus']['Status']
        if status == 'SUCCEEDED':
            if current() != after:
                raise RuntimeError('Policy changed during provisioning; inspect without overwriting')
            print(json.dumps({'provisioned': True, 'requestId': request,
                              'policySha256': digest(after), 'otherStatementsPreserved': True}))
            return
        if status == 'FAILED':
            raise RuntimeError('Provisioning failed; inspect request ' + request)
        time.sleep(3)
    raise RuntimeError('Provisioning pending; inspect request ' + request)


if __name__ == '__main__':
    main()
