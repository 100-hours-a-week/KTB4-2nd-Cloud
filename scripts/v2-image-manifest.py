#!/usr/bin/env python3
"""Validate/update V2 candidates; verify registry images without deploying them.

ai-app is shared by AI API and process runner. V1 manifests are never read.
Local validation proves identity/format consistency, not source provenance or
service compatibility. The V2 integration gate must establish compatibility.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / 'deploy/v2/image-manifest.json'
COMPONENTS = {
    'frontend': ('100-hours-a-week/KTB4-2nd-FE', 'yeodam-frontend'),
    'backend': ('100-hours-a-week/KTB4-2nd-BE', 'yeodam-backend'),
    'ai-app': ('100-hours-a-week/KTB4-2nd-AI', 'yeodam-ai-app'),
}
FIELDS = {'sourceRepository', 'sourceSha', 'image', 'digest', 'platform'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f'Duplicate JSON key: {key}')
        result[key] = value
    return result


def validate(data):
    require(isinstance(data, dict) and set(data) == {'schemaVersion', 'components'},
            'Expected schemaVersion and components only')
    require(type(data['schemaVersion']) is int and data['schemaVersion'] == 1,
            'Unsupported schemaVersion')
    components = data['components']
    require(isinstance(components, dict) and set(components) == set(COMPONENTS),
            'Expected frontend, backend and ai-app')
    for name, (repository, package) in COMPONENTS.items():
        entry = components[name]
        require(isinstance(entry, dict) and set(entry) == FIELDS,
                f'{name}: unexpected or missing fields')
        require(all(isinstance(v, str) for v in entry.values()),
                f'{name}: values must be strings')
        require(entry['sourceRepository'] == repository, f'{name}: wrong source repository')
        require(re.fullmatch(r'[0-9a-f]{40}', entry['sourceSha']), f'{name}: invalid source SHA')
        expected = f"ghcr.io/100-hours-a-week/{package}:sha-{entry['sourceSha']}"
        require(entry['image'] == expected, f'{name}: image package or SHA does not match')
        require(re.fullmatch(r'sha256:[0-9a-f]{64}', entry['digest']), f'{name}: invalid digest')
        require(entry['platform'] == 'linux/amd64', f'{name}: expected linux/amd64')
    return data


def load(path):
    return validate(json.loads(path.read_text(), object_pairs_hook=unique_object))


def update(path, args):
    data = load(path)
    data['components'][args.component] = {
        'sourceRepository': args.source_repository,
        'sourceSha': args.source_sha,
        'image': args.image,
        'digest': args.digest,
        'platform': 'linux/amd64',
    }
    validate(data)  # Invalid events must leave the original file intact.
    content = json.dumps(data, indent=2) + '\n'
    if path.read_text() == content:
        print(f'{args.component}: unchanged')
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f'{args.component}: updated {args.source_sha}')


def verify(data):
    for name, entry in data['components'].items():
        result = subprocess.run(
            ['docker', 'buildx', 'imagetools', 'inspect', entry['image'],
             '--format', '{{.Manifest.Digest}}'],
            capture_output=True, text=True, check=True,
        )
        require(result.stdout.strip() == entry['digest'], f'{name}: registry tag/digest mismatch')
        # Pull immutable content for the required platform, as the V1 image gate does.
        reference = entry['image'].split(':sha-', 1)[0] + '@' + entry['digest']
        subprocess.run(['docker', 'pull', '--platform', entry['platform'], reference],
                       stdout=subprocess.DEVNULL, check=True)
        print(f'{name}: registry digest and linux/amd64 pull verified', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('validate', help='Offline manifest identity/format check')
    commands.add_parser('verify', help='Check GHCR SHA tags/digests and pull linux/amd64 images')
    edit = commands.add_parser('update', help='Update one component; preserve the other two')
    edit.add_argument('--component', choices=COMPONENTS, required=True)
    for argument in ['source-repository', 'source-sha', 'image', 'digest']:
        edit.add_argument('--' + argument, required=True)
    args = parser.parse_args()
    if args.command == 'update':
        update(args.manifest, args)
    elif args.command == 'verify':
        verify(load(args.manifest))
    else:
        load(args.manifest)
        print('V2 image manifest identity/format validated')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'V2 image manifest failed: {exc}', file=sys.stderr)
        sys.exit(1)
