#!/usr/bin/env python3
"""Select integration stacks only for reviewed inputs; unknown paths require both."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess


# Keep these lists narrow. Unlisted deployment/configuration files require both.
V1_FILES = frozenset({
    'app-compose.yml', 'worker-compose.yml',
    'deploy/image-versions.env', 'deploy/image-versions.env.example',
    'scripts/validate-image-versions.sh', 'scripts/verify-release-images.sh',
    'tests/ci/compose.yml', 'tests/ci/ec2-stub.py', 'tests/ci/seed.sql',
    'tests/ci/smoke.py', 'tests/ci/run-integration.sh',
})
V2_FILES = frozenset({
    'scripts/v2-image-manifest.py', 'tests/ci/test_v2_image_manifest.py',
    'deploy/v2-staging/kafka/outbox-connector.json',
})
V2_PREFIXES = ('deploy/v2/', 'tests/ci/v2/')


CONNECT_PREFIXES = ('deploy/v3/connect-image/', 'deploy/v3/connect/')
CONNECT_FILES = frozenset({
    'deploy/v3/kafka-operator/upstream.lock.json',
    'scripts/prepare-v3-connect-image.py', 'scripts/validate-v3-connect-image.sh',
    'tests/ci/smoke-v3-connect.py', 'tests/ci/v3/CheckConnectPlugins.java',
    '.github/workflows/v3-connect-check.yml', '.github/workflows/v3-connect-image.yml',
})


SCHEMA_PREFIXES = ('deploy/v3/mysql-schema-image/', 'deploy/v3/mysql-schema/', 'deploy/v3/mysql/')
SCHEMA_FILES = frozenset(('scripts/v2_v3_photo_migration.py','scripts/migrate-v2-photos.py','scripts/migration/photo-references.sql','scripts/migration/photo-sdk-requirements.txt','tests/ci/test_v2_v3_photos.py','scripts/export-v2-mysql-backup.py','scripts/restore-v2-mysql-backup.py','scripts/run-v2-mysql-backup.py','tests/ci/test_v2_mysql_backup.py','scripts/v2_v3_mysql_logical.py','tests/ci/test_v2_v3_mysql_logical.py','tests/ci/smoke-v2-v3-mysql-logical.py','scripts/check-v3-mysql-schema.py','tests/ci/test_v3_mysql_schema.py','tests/ci/v3/mysql-schema.values.fixture.yaml','deploy/v3/environments/foundation/mysql-schema.values.yaml','deploy/v3/environments/foundation/mysql-cdc-secrets.values.yaml','scripts/prepare-v3-mysql-schema-image.py', 'scripts/manage-v3-mysql-cdc.py', 'scripts/create-v3-mysql-bootstrap-secret.py', 'scripts/validate-v3-mysql-schema-image.sh', 'tests/ci/smoke-v3-mysql-schema.py', '.github/workflows/v3-mysql-schema-check.yml', '.github/workflows/v3-mysql-schema-image.yml'))

OUTBOX_PREFIXES = ('deploy/v3/outbox/', 'deploy/v3/connect/', 'deploy/v3/connect-image/', 'deploy/v3/mysql/', 'deploy/v3/kafka-access/')
OUTBOX_FILES = frozenset(('scripts/v3_outbox_contract.py', 'scripts/check-v3-outbox.py', 'scripts/prepare-v3-outbox-ca.py', 'scripts/validate-v3-outbox.sh', 'scripts/prepare-v3-connect-image.py', 'scripts/manage-v3-mysql-cdc.py', 'scripts/create-v3-mysql-bootstrap-secret.py', 'tests/ci/smoke-v3-connect.py', 'tests/ci/smoke-v3-outbox.py', 'tests/ci/v3/OutboxWire.java', 'tests/ci/test_v3_outbox.py', 'deploy/v3/environments/foundation/connect-outbox.values.yaml', 'deploy/v3/environments/foundation/outbox.values.yaml', 'deploy/v3/environments/foundation/outbox-secrets.values.yaml', '.github/workflows/v3-outbox-check.yml', '.github/workflows/v3-connect-image.yml'))

def path_scope(path):
    # Runtime directories take priority over documentation extensions.
    if path in V1_FILES:
        return 'v1'
    if path in V2_FILES or path.startswith(V2_PREFIXES):
        return 'v2'
    if path.endswith(('.md', '.pdf')):
        return 'documentation'
    return 'both'


def select(paths):
    files = [{'path': path, 'scope': path_scope(path)} for path in sorted(set(paths))]
    scopes = {entry['scope'] for entry in files}
    # An empty/unexplained change must not silently skip runtime verification.
    v1 = not files or bool(scopes & {'v1', 'both'})
    v2 = not files or bool(scopes & {'v2', 'both'})
    return {'runtime_required': v1, 'v2_runtime_required': v2,
            'outbox_required': not files or any(e['path'] in OUTBOX_FILES or e['path'].startswith(OUTBOX_PREFIXES) for e in files),
            'schema_image_required': not files or any(e['path'] in SCHEMA_FILES or e['path'].startswith(SCHEMA_PREFIXES) for e in files),
            'connect_image_required': not files or any(e['path'] in CONNECT_FILES or e['path'].startswith(CONNECT_PREFIXES) for e in files),
            'reason': 'changed-files' if files else 'empty-diff-requires-both', 'files': files}


def changed_paths(base, head, cwd=None):
    for sha in (base, head):
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', sha):
            raise ValueError('base/head must be full commit SHAs')
    merge_bases = subprocess.check_output(
        ['git', 'merge-base', '--all', base, head], cwd=cwd).splitlines()
    if len(merge_bases) != 1:
        raise ValueError('exactly one PR merge base is required')
    # --no-renames presents BOTH old and new names for a rename or move.
    # NUL separation supports spaces/newlines without truncating the changed set.
    data = subprocess.check_output([
        'git', 'diff', '--no-ext-diff', '--no-renames', '--name-only', '-z',
        merge_bases[0].decode('ascii'), head, '--'], cwd=cwd)
    return [os.fsdecode(path) for path in data.split(b'\0') if path]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True)
    parser.add_argument('--head', required=True)
    parser.add_argument('--output', type=Path, help='GitHub step output file')
    parser.add_argument('--report', type=Path, help='JSON decision report')
    args = parser.parse_args()
    # Git errors fail the validation job before any outputs are emitted.
    result = select(changed_paths(args.base, args.head))
    encoded = json.dumps(result, ensure_ascii=True, indent=2) + '\n'
    if args.report:
        args.report.write_text(encoded, encoding='utf-8')
    if args.output:
        with args.output.open('a', encoding='utf-8') as stream:
            for key in ('runtime_required', 'v2_runtime_required', 'connect_image_required', 'schema_image_required', 'outbox_required'):
                stream.write(f'{key}={str(result[key]).lower()}\n')
    print(encoded, end='')


if __name__ == '__main__':
    main()
