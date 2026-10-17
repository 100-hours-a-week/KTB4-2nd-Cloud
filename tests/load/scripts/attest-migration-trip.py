#!/usr/bin/env python3
"""Emit counts and hashes for a scoped trip run without exporting row payloads."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sys


FIELDS = ('tripId', 'userId', 'tripName', 'startDate', 'endDate', 'status',
          'regionId', 'regionCode', 'regionNameHex', 'latitude', 'longitude')


def digest(items):
    value = hashlib.sha256()
    for item in sorted(items):
        value.update(json.dumps(item, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        value.update(b'\n')
    return value.hexdigest()


def attest_events(path, run_id):
    started, final = {}, {}
    plan = None
    for line in path.read_text(encoding='utf-8').splitlines():
        event = json.loads(line[line.index('{'):])
        if event.get('source') != 'migration-trip-create' or event.get('runId') != run_id:
            raise ValueError('Unexpected event source or run')
        kind = event['event']
        if kind == 'run_started':
            if plan is not None:
                raise ValueError('Duplicate plan')
            plan = event
            continue
        if kind not in ('started', 'final'):
            raise ValueError('Unexpected event kind')
        key = (event['userId'], event['tripName'])
        selected = started if kind == 'started' else final
        if key in selected:
            raise ValueError('Duplicate request event')
        selected[key] = event
    if not plan or plan.get('mode') != 'load' or plan.get('rate') != 30 or plan.get('duration') != 120:
        raise ValueError('Expected one 30 RPS / 120s plan')
    if len(started) not in (3600, 3601) or set(started) != set(final):
        raise ValueError('Incomplete request events')
    projected = []
    trip_ids = set()
    for key, first in started.items():
        last = final[key]
        trip_id = last.get('tripId')
        if last.get('outcome') != 'confirmed' or last.get('status') != 201 or not isinstance(trip_id, int):
            raise ValueError('Unconfirmed HTTP response')
        if trip_id in trip_ids:
            raise ValueError('Duplicate response trip ID')
        trip_ids.add(trip_id)
        request = first['request']
        codes = request['regionCodes']
        if request['tripName'] != key[1] or not codes or len(codes) != len(set(codes)):
            raise ValueError('Invalid request regions')
        for code in codes:
            projected.append((str(trip_id), str(key[0]), key[1], request['startDate'],
                              request['endDate'], 'PROCESSING', str(code)))
    return {'runId': run_id, 'kind': 'events', 'trips': len(started),
            'regions': len(projected), 'projectionSha256': digest(projected)}


def attest_rows(path, run_id, max_trip_id=None):
    rows, projected, groups = [], [], {}
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        for line in stream:
            fields = line.rstrip('\n').split('\t')
            if len(fields) != len(FIELDS):
                raise ValueError('Unexpected TSV column count')
            row = dict(zip(FIELDS, fields))
            if not row['tripId'].isdigit() or not row['regionId'].isdigit() or not row['userId'].isdigit():
                raise ValueError('Invalid row ID')
            if max_trip_id is not None and int(row['tripId']) > max_trip_id:
                continue
            if row['status'] != 'PROCESSING' or not row['regionNameHex']:
                raise ValueError('Invalid trip or region data')
            key = (row['userId'], row['tripName'])
            group = groups.setdefault(key, {'tripIds': set(), 'regionIds': set(), 'codes': set()})
            group['tripIds'].add(row['tripId'])
            if row['regionId'] in group['regionIds'] or row['regionCode'] in group['codes']:
                raise ValueError('Duplicate region')
            group['regionIds'].add(row['regionId'])
            group['codes'].add(row['regionCode'])
            rows.append(tuple(fields))
            projected.append(tuple(row[field] for field in FIELDS[:6]) + (row['regionCode'],))
    if not rows or any(len(group['tripIds']) != 1 for group in groups.values()):
        raise ValueError('Missing rows or duplicate trip')
    return {'runId': run_id, 'kind': 'rows', 'trips': len(groups), 'regions': len(rows),
            'minTripId': min(int(row[0]) for row in rows), 'maxTripId': max(int(row[0]) for row in rows),
            'projectionSha256': digest(projected), 'fullRowsSha256': digest(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=('events', 'rows'), required=True)
    parser.add_argument('--path', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--max-trip-id', type=int, help='For comparing the pre-switch Source prefix on Target')
    args = parser.parse_args()
    report = attest_events(args.path, args.run_id) if args.kind == 'events' else attest_rows(args.path, args.run_id, args.max_trip_id)
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        raise SystemExit(2)
