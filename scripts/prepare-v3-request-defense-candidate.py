#!/usr/bin/env python3
"""Enable the existing authenticated user limiter only; no network, new images or live changes."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import v3_release_bundle as bundle

TARGETS = ('trip-api', 'photo-api')
KEY = 'REQUEST_RATE_LIMIT_ENABLED'


def prepare(source):
    bundle.prepared(source)
    result = deepcopy(source)
    backend = result['values']['backend']
    bundle.require(backend.get('backendConnectionContract'), 'Connected BE profile required')
    for name, workload in backend['workloads'].items():
        config = bundle.apps.merge(backend['defaults'], workload)['configMapData']
        if name not in TARGETS:
            bundle.require(KEY not in config, 'Do not enable limiter on unrelated roles')
        else:
            bundle.require(config.get(KEY) in (None, 'true'), 'Unreviewed existing limiter setting')
            workload.setdefault('configMapData', {})[KEY] = 'true'
    bundle.prepared(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = prepare(bundle.parse_json(args.candidate.read_text()))
    with args.output.open('x') as handle:
        handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
    print('Prepared trip/photo API limiter opt-in; unchanged images/resources; not deployed')


if __name__ == '__main__':
    main()
