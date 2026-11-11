#!/usr/bin/env python3
"""Prepare an opt-in APM-only candidate. No AWS, registry, Git writes or deployment."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import v3_release_bundle as bundle

BE_ROLES = ['auth-api', 'trip-api', 'photo-api', 'photo-converter', 'trip-result-worker']
AI_ROLES = ['api', 'runner']


def prepare(source):
    bundle.prepared(source)
    result = deepcopy(source)
    for profile, roles in [('backend', BE_ROLES), ('ai', AI_ROLES)]:
        current = result['values'][profile].get('apmWorkloads', [])
        bundle.require(isinstance(current, list) and len(current) == len(set(current))
                       and set(current) <= set(roles), 'Unsupported existing APM selection')
        result['values'][profile]['apmWorkloads'] = list(roles)
    bundle.prepared(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = prepare(bundle.parse_json(args.candidate.read_text()))
    # Create only: never silently replace a reviewed candidate or input file.
    with args.output.open('x') as output:
        output.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
    print('APM-only candidate prepared; images/resources/config revisions preserved; not deployed')


if __name__ == '__main__':
    main()
