#!/usr/bin/env python3
"""Prepare approved FE GitOps inputs; no deployment occurs by default."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import v3_gitops_release as release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--pr')
    group.add_argument('--rollback')
    parser.add_argument('--expected-previous', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--approved-release-id')
    args = parser.parse_args()
    try:
        if args.publish:
            result = release.publish(args.pr, args.rollback, args.expected_previous, args.approved_release_id)
        else:
            files, result = release.prepare(args.pr, args.rollback, args.expected_previous)
            if args.directory.exists():
                raise ValueError('Refusing to overwrite a previous preview')
            args.directory.mkdir(parents=True)
            for name, value in files.items():
                path = args.directory / 'bundle' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
        args.directory.mkdir(parents=True, exist_ok=True)
        (args.directory / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
        if not args.publish and os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write('publication_enabled=' + str(result['publicationEnabled']).lower() + '\n')
                output.write('release_id=' + result['releaseId'] + '\n')
        print(json.dumps(result))
    except (ValueError, KeyError, OSError, TypeError, subprocess.CalledProcessError) as error:
        reason = str(error) if isinstance(error, ValueError) else 'Evidence lookup or rendering failed (' + type(error).__name__ + ')'
        result = {'status': 'rejected', 'published': False, 'deployed': False, 'reason': reason}
        if not args.directory.exists():
            args.directory.mkdir(parents=True)
            (args.directory / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
