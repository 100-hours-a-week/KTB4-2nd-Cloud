#!/usr/bin/env python3
"""Reconcile trip-create k6 events with scoped Source/Target trip-region TSV exports."""

import argparse
import json
from pathlib import Path
import sys
from uuid import UUID


FIELDS = ('tripId', 'userId', 'tripName', 'startDate', 'endDate', 'status',
          'regionId', 'regionCode', 'regionNameHex', 'latitude', 'longitude')


def read_events(path, run_id):
    started, final = {}, {}
    for line in path.read_text(encoding='utf-8').splitlines():
        offset = line.find('{')
        if offset < 0:
            if line.strip():
                raise ValueError('Non-JSON event line')
            continue
        event = json.loads(line[offset:])
        if event.get('source') != 'migration-trip-create' or event.get('runId') != run_id:
            raise ValueError('Unexpected event source or run')
        key = (event['userId'], event['tripName'])
        if event['event'] == 'started':
            if key in started:
                raise ValueError('Duplicate started event')
            started[key] = event['request']
        elif event['event'] == 'final':
            if key in final:
                raise ValueError('Duplicate final event')
            final[key] = event
    if not started or set(started) != set(final):
        raise ValueError('Every started request needs one final event')
    return started, final


def read_rows(path):
    rows = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        fields = line.split('\t')
        if len(fields) != len(FIELDS):
            raise ValueError(f'{path}: expected {len(FIELDS)} TSV columns')
        row = dict(zip(FIELDS, fields))
        key = (int(row['userId']), row['tripName'])
        rows.setdefault(key, []).append(row)
    return rows


def validate_rows(rows, request, user_id):
    if not rows:
        return False, 'not_observed'
    trip_ids = {row['tripId'] for row in rows}
    region_ids = {row['regionId'] for row in rows}
    codes = [row['regionCode'] for row in rows]
    if len(trip_ids) != 1 or len(region_ids) != len(rows) or not all(value.isdigit() for value in region_ids):
        return False, 'duplicate_trip_or_region'
    if any(row['userId'] != str(user_id) or row['tripName'] != request['tripName'] or
           row['startDate'] != request['startDate'] or row['endDate'] != request['endDate'] or
           row['status'] != 'PROCESSING' or row['tripId'] == '' or
           row['regionNameHex'] == '' for row in rows):
        return False, 'payload_mismatch'
    if sorted(codes) != sorted(request['regionCodes']):
        return False, 'region_mismatch'
    return True, next(iter(trip_ids))


def reconcile(events, source, target, run_id):
    started, final = read_events(events, run_id)
    source_rows, target_rows = read_rows(source), read_rows(target)
    results = []
    for key, request in started.items():
        user_id, trip_name = key
        source_ok, source_detail = validate_rows(source_rows.get(key, []), request, user_id)
        target_ok, target_detail = validate_rows(target_rows.get(key, []), request, user_id)
        same_rows = sorted(tuple(row[field] for field in FIELDS) for row in source_rows.get(key, [])) == \
            sorted(tuple(row[field] for field in FIELDS) for row in target_rows.get(key, []))
        response = final[key]
        http_confirmed = response['outcome'] == 'confirmed' and response['status'] == 201 and \
            isinstance(response['tripId'], int)
        committed = source_ok and target_ok and same_rows
        passed = committed and http_confirmed and str(response['tripId']) == source_detail
        results.append({'userId': user_id, 'tripName': trip_name,
                        'httpStatus': response['status'], 'httpOutcome': response['outcome'],
                        'source': source_detail, 'target': target_detail,
                        'sourceTargetEqual': same_rows,
                        'classification': 'confirmed_and_replicated' if passed else
                        'committed_without_confirmed_http' if committed and not http_confirmed else
                        'not_observed' if source_detail == target_detail == 'not_observed' else 'mismatch',
                        'passed': passed})
    unexpected = sorted((set(source_rows) | set(target_rows)) - set(started))
    return {'runId': run_id, 'requests': results, 'unexpectedRows': len(unexpected),
            'passed': bool(results) and not unexpected and all(row['passed'] for row in results)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', required=True, type=Path)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--target', required=True, type=Path)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if str(UUID(args.run_id)) != args.run_id:
        raise ValueError('Use a canonical run UUID')
    report = reconcile(args.events, args.source, args.target, args.run_id)
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        raise SystemExit(2)
