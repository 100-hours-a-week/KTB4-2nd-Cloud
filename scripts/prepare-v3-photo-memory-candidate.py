#!/usr/bin/env python3
"""Prepare the bounded converter memory profile; no cluster, image or Secret access."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import v3_release_bundle as bundle
from v3_backend_contract import CONTRACT, CONNECTION


def prepare(source):
    bundle.prepared(source)
    result = deepcopy(source)
    profile = result['values']['backend']
    bundle.require(profile.get('backendConnectionContract'), 'Connected BE profile required')
    workload = profile['workloads']['photo-converter']
    merged = bundle.apps.merge(profile['defaults'], workload)
    memory = CONTRACT['photoConverterMemory']
    bundle.require(merged['resources']['limits']['memory'] == memory['containerMemoryLimit'],
                   'Keep the existing 1Gi converter limit')
    data = merged['configMapData']
    java = CONNECTION['javaToolOptions'].replace(CONTRACT['javaToolOptions'], memory['javaToolOptions'], 1)
    bundle.require(data['JAVA_TOOL_OPTIONS'] in (CONNECTION['javaToolOptions'], java), 'Unreviewed existing JVM options')
    bundle.require(data.get('MALLOC_ARENA_MAX') in (None, memory['allocatorArenas']), 'Unreviewed allocator setting')
    bundle.require(data.get('VIPS_CONCURRENCY') == '1', 'Keep single-threaded libvips')
    workload.setdefault('configMapData', {}).update(JAVA_TOOL_OPTIONS=java, MALLOC_ARENA_MAX=memory['allocatorArenas'])
    bundle.prepared(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = prepare(bundle.parse_json(args.candidate.read_text()))
    with args.output.open('x') as output:
        output.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
    print('Converter-only memory candidate prepared; existing images/resources/admission retained; not deployed')


if __name__ == '__main__':
    main()
