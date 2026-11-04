#!/usr/bin/env python3
"""Check a private Terraform JSON plan without printing State or planned values."""
import argparse
from collections import Counter
import json
from pathlib import Path

ACCOUNT = '483175530259'
PROTECTED_IDS = {
    'i-05e1ad9dd96dba4ae', 'vol-05d2497eaeabb57d3',
    'vpc-0d84786e134e32933', 'subnet-0f5d19ddb40fbc262',
    'subnet-0a1454391e0f12f22', 'rtb-0fc40e73348be69f0',
    'nat-0d0a833fa2e491f5c', 'sg-0a80da95d51c138fe', 'sg-07c2fc1ee8bd5858e',
}
ARCHIVES = {
    f'yeodam-v2-staging-load-test-{ACCOUNT}-ap-northeast-2',
    f'yeodam-v2-staging-mysql-backup-{ACCOUNT}-ap-northeast-2',
    f'yeodam-v2-stg-v1-app-{ACCOUNT}-ap-northeast-2',
}
SERVICE_BUCKET = f'yeodam-v2-staging-app-data-{ACCOUNT}-ap-northeast-2'
UPDATES = {'aws_s3_bucket_lifecycle_configuration.load_test',
           'aws_s3_bucket_lifecycle_configuration.mysql_backup'}
PROTECTED_ADDRESSES = {
    'aws_instance.ai_gpu', 'aws_ebs_volume.qdrant_data', 'aws_volume_attachment.qdrant_data',
    'aws_iam_role.ai_gpu', 'aws_iam_instance_profile.ai_gpu',
    'aws_iam_role_policy_attachment.ai_gpu_ssm', 'aws_iam_role.staging_release_deploy',
    'aws_security_group.ai_gpu', 'aws_security_group.ai_kafka_clients',
    'aws_vpc_security_group_egress_rule.ai_gpu_https',
}


def validate(plan):
    if plan.get('errored') or plan.get('complete') is False:
        raise ValueError('Plan is incomplete or errored')
    changes = []
    observed = set()
    for row in plan.get('resource_changes', []):
        if row.get('mode') == 'data':
            continue
        address, change = row['address'], row['change']
        before = change.get('before') or {}
        actions = change['actions']
        observed.add(address)
        if actions == ['no-op']:
            continue
        if address in PROTECTED_ADDRESSES or before.get('id') in PROTECTED_IDS:
            raise ValueError('Protected shared GPU resource would change: ' + address)
        if actions == ['update'] and address in UPDATES:
            # Preservation must disable expiration rather than alter the bucket.
            after = change.get('after') or {}
            if before.get('bucket') != after.get('bucket'):
                raise ValueError('Archive bucket identity changed')
            rules = after.get('rule', [])
            for rule in rules:
                if (rule.get('expiration') or rule.get('noncurrent_version_expiration')) and rule['status'] != 'Disabled':
                    if address.endswith('.load_test') and rule['id'] == 'expire-fixtures-after-one-day':
                        continue
                    raise ValueError('Archive expiration remains enabled')
        elif actions == ['delete']:
            if before.get('bucket') in ARCHIVES:
                raise ValueError('Archive resource would be deleted: ' + address)
            if row['type'] == 'aws_s3_bucket' and before.get('id') != SERVICE_BUCKET:
                raise ValueError('Unexpected bucket deletion')
            serialized = json.dumps(before)
            if 'yeodam-v2-production' in serialized or 'yeodam/v2/production' in serialized:
                raise ValueError('Production reference in deletion: ' + address)
        else:
            raise ValueError('Unexpected creation, replacement or update: ' + address)
        changes.append((address, actions))
    if not PROTECTED_ADDRESSES.issubset(observed):
        raise ValueError('Shared GPU identities missing from plan')
    return dict(Counter(','.join(actions) for _, actions in changes))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan', type=Path)
    args = parser.parse_args()
    print(json.dumps({'validatedActions': validate(json.loads(args.plan.read_text()))}))


if __name__ == '__main__':
    main()
