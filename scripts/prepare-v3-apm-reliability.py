#!/usr/bin/env python3
"""Opt-in telemetry-only candidate; no images, resources, Secret revisions or Git writes."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import v3_release_bundle as bundle


def prepare(source):
    bundle.prepared(source)
    result = deepcopy(source)
    for profile, roles in [('backend', ['auth-api','trip-api','photo-api','photo-converter','trip-result-worker']),
                           ('ai', ['api','runner'])]:
        bundle.require(set(result['values'][profile].get('apmWorkloads', [])) == set(roles),
                       'Instrumented readers must already be deployed on every role')
        result['values'][profile]['apmReliabilityEnabled'] = True
    bundle.prepared(result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidate', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    result = prepare(bundle.parse_json(a.candidate.read_text()))
    with a.output.open('x') as f:
        f.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)+'\n')
    print('Telemetry profile prepared; images/resources/Secret revisions preserved; not deployed')


if __name__ == '__main__': main()
