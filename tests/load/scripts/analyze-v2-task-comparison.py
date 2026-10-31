#!/usr/bin/env python3
"""Aggregate native k6 samples without mixing warm-up and measured rates."""
import argparse
import collections
import hashlib
import json
from pathlib import Path


def percentile(values, q):
    if not values: return None
    ordered = sorted(values)
    p = (len(ordered) - 1) * q
    a = int(p)
    return ordered[a] + (ordered[min(a + 1, len(ordered) - 1)] - ordered[a]) * (p - a)


def analyze(directory):
    durations = collections.defaultdict(list)
    counts = collections.Counter()
    codes = collections.defaultdict(collections.Counter)
    for line in (directory / 'metrics.json').open():
        row = json.loads(line)
        if row.get('type') != 'Point': continue
        data = row['data']; tags = data.get('tags', {})
        phase = tags.get('phase', tags.get('scenario', 'unknown'))
        operation = tags.get('operation', 'unknown')
        if row['metric'] == 'http_req_duration':
            durations[(phase, operation)].append(data['value'])
            codes[phase][tags.get('status', 'unknown')] += 1
        elif row['metric'] in ('dropped_iterations', 'business_failures'):
            counts[(phase, row['metric'])] += data['value']
    phases = {}
    for phase in ['warmup', 'r10', 'r30', 'r50']:
        phases[phase] = {'statusCounts': dict(codes[phase]),
            'dropped': counts[(phase, 'dropped_iterations')],
            'businessFailures': counts[(phase, 'business_failures')], 'operations': {}}
        for operation in ['trip_list', 'trip_create']:
            v = durations[(phase, operation)]
            phases[phase]['operations'][operation] = {'requests': len(v),
                'meanMs': sum(v) / len(v) if v else None,
                'p95Ms': percentile(v, .95), 'p99Ms': percentile(v, .99),
                'maxMs': max(v) if v else None}
    events = []
    for line in (directory / 'events.jsonl').open():
        try: row = json.loads(line)
        except ValueError: continue
        if row.get('event') == 'result' and row.get('operation') == 'trip_create': events.append(row)
    confirmed = [e for e in events if e['ok']]
    ids = [e['tripId'] for e in confirmed]
    projection = ''.join(f"{e['tripId']}\t{e['userId']}\t{e['tripName']}\t50110\n"
                         for e in sorted(confirmed, key=lambda e: e['tripId']))
    return {'start': (directory / 'start.txt').read_text().strip(),
        'end': (directory / 'end.txt').read_text().strip(),
        'exitCode': int((directory / 'exit-code.txt').read_text()), 'phases': phases,
        'writeAttempts': len(events), 'confirmedWrites': len(confirmed),
        'duplicateResponseIds': len(ids) - len(set(ids)),
        'confirmedProjectionSHA256': hashlib.sha256(projection.encode()).hexdigest()}


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('directory', type=Path)
    args = p.parse_args()
    result = analyze(args.directory)
    (args.directory / 'analysis.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
