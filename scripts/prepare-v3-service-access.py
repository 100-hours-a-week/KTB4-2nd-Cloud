#!/usr/bin/env python3
"""Prepare or recheck public Service references offline. No kubeconfig, credentials or writes to a cluster."""
import argparse
import json
import os
from pathlib import Path
from v3_service_access import require, review, values


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--services', required=True, type=Path, help='Fresh public List of the two Service objects')
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument('--directory', type=Path)
    target.add_argument('--expected-review', type=Path, help='Recheck UID, IP, selector and port immediately before apply')
    a = p.parse_args()
    evidence = review(json.loads(a.services.read_text()))
    if a.expected_review:
        require(evidence == json.loads(a.expected_review.read_text()), 'Service identity or address changed; prepare and review again')
        print('Current Service identities/addresses match the reviewed input; no deployment performed')
        return
    require(not a.directory.exists(), 'Refusing overwrite')
    os.umask(0o077)
    a.directory.mkdir(parents=True)
    for name, data in [('values.json', values(evidence)), ('review.json', evidence)]:
        (a.directory / name).write_text(json.dumps(data, indent=2) + '\n')
    print('Prepared three exact Service egress aliases from public metadata; no source reads or deployment')


if __name__ == '__main__':
    main()
