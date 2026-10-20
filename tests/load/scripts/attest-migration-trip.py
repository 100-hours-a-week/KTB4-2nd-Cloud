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


def attest_events(path, run_id, expected_rate=30, allow_incomplete=False,
                  expected_duration=120, expected_mode='load'):
    started, final = {}, {}
    refreshes = refresh_failures = 0
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
        if kind == 'auth_refreshed':
            refreshes += 1
            continue
        if kind == 'auth_refresh_failed':
            refresh_failures += 1
            continue
        if kind not in ('started', 'final'):
            raise ValueError('Unexpected event kind')
        key = (event['userId'], event['tripName'])
        selected = started if kind == 'started' else final
        if key in selected:
            raise ValueError('Duplicate request event')
        selected[key] = event
    if (not plan or plan.get('mode') != expected_mode or plan.get('rate') != expected_rate or
            plan.get('duration') != expected_duration):
        raise ValueError(f'Expected one {expected_rate} RPS / {expected_duration}s {expected_mode} plan')
    if not started or len(started) > expected_rate * expected_duration + 1 or set(final) - set(started):
        raise ValueError('Incomplete request events')
    if not allow_incomplete and (len(started) < expected_rate * expected_duration or
                                 set(started) != set(final) or refresh_failures):
        raise ValueError('Incomplete request events')
    projected, request_projected = [], []
    trip_ids = set()
    for key, first in started.items():
        request = first['request']
        codes = request['regionCodes']
        if request['tripName'] != key[1] or not codes or len(codes) != len(set(codes)):
            raise ValueError('Invalid request regions')
        last = final.get(key)
        confirmed = last is not None and last.get('outcome') == 'confirmed' and last.get('status') == 201 \
            and isinstance(last.get('tripId'), int)
        if not allow_incomplete and not confirmed:
            raise ValueError('Unconfirmed HTTP response')
        if confirmed:
            trip_id = last['tripId']
            if trip_id in trip_ids:
                raise ValueError('Duplicate response trip ID')
            trip_ids.add(trip_id)
        for code in codes:
            request_projected.append((str(key[0]), key[1], request['startDate'],
                                      request['endDate'], 'PROCESSING', str(code)))
            if confirmed:
                projected.append((str(trip_id),) + request_projected[-1])
    return {'runId': run_id, 'kind': 'events', 'trips': len(started),
            'regions': len(request_projected), 'final': len(final), 'confirmed': len(trip_ids),
            'unconfirmed': len(final) - len(trip_ids), 'missingFinal': len(started) - len(final),
            'authRefreshes': refreshes, 'authRefreshFailures': refresh_failures,
            'requestProjectionSha256': digest(request_projected),
            'projectionSha256': digest(projected) if len(trip_ids) == len(started) else None}


def attest_rows(path, run_id, max_trip_id=None, allow_empty=False):
    rows, projected, request_projected, groups = [], [], [], {}
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
            request_projected.append(tuple(row[field] for field in FIELDS[1:6]) + (row['regionCode'],))
    if (not rows and not allow_empty) or any(len(group['tripIds']) != 1 for group in groups.values()):
        raise ValueError('Missing rows or duplicate trip')
    return {'runId': run_id, 'kind': 'rows', 'trips': len(groups), 'regions': len(rows),
            'minTripId': min((int(row[0]) for row in rows), default=None),
            'maxTripId': max((int(row[0]) for row in rows), default=None),
            'requestProjectionSha256': digest(request_projected),
            'projectionSha256': digest(projected), 'fullRowsSha256': digest(rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=('events', 'rows'), required=True)
    parser.add_argument('--path', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--max-trip-id', type=int, help='For comparing the pre-switch Source prefix on Target')
    parser.add_argument('--expected-rate', type=int, choices=(10, 30, 50, 100), default=30)
    parser.add_argument('--expected-duration', type=int, default=120)
    parser.add_argument('--expected-mode', choices=('load', 'live', 'original', 'bulk', 'a-rehearsal'), default='load')
    parser.add_argument('--allow-incomplete', action='store_true',
                        help='Keep aggregate evidence for a stopped or failed run; never implies a passed load')
    args = parser.parse_args()
    report = (attest_events(args.path, args.run_id, args.expected_rate, args.allow_incomplete,
                            args.expected_duration, args.expected_mode)
              if args.kind == 'events' else attest_rows(args.path, args.run_id, args.max_trip_id, args.allow_incomplete))
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        raise SystemExit(2)
