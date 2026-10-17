#!/usr/bin/env python3
"""Reconcile trip-create k6 events with scoped Source/Target trip-region TSV exports."""

import argparse
import gzip
import json
from pathlib import Path
import sys
from uuid import UUID


FIELDS = ('tripId', 'userId', 'tripName', 'startDate', 'endDate', 'status',
          'regionId', 'regionCode', 'regionNameHex', 'latitude', 'longitude')


def read_events(path, run_id):
    started, final = {}, {}
    plan = None
    for line in path.read_text(encoding='utf-8').splitlines():
        offset = line.find('{')
        if offset < 0:
            if line.strip():
                raise ValueError('Non-JSON event line')
            continue
        event = json.loads(line[offset:])
        if event.get('source') != 'migration-trip-create' or event.get('runId') != run_id:
            raise ValueError('Unexpected event source or run')
        if event['event'] == 'run_started':
            if plan is not None:
                raise ValueError('Duplicate run plan')
            plan = event
            continue
        if event['event'] not in ('started', 'final'):
            raise ValueError('Unexpected trip event')
        key = (event['userId'], event['tripName'])
        if event['event'] == 'started':
            if key in started:
                raise ValueError('Duplicate started event')
            started[key] = event['request']
        else:
            if key in final:
                raise ValueError('Duplicate final event')
            final[key] = event
    if not started or set(final) - set(started):
        raise ValueError('Final event without started request')
    return started, final, plan


def read_rows(path):
    rows = {}
    opener = gzip.open if str(path).endswith('.gz') else open
    with opener(path, 'rt', encoding='utf-8') as stream:
        for line in stream:
            fields = line.rstrip('\n').split('\t')
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


def reconcile(events, source, target, run_id, mode='baseline', summary=None, expected_rate=10):
    if mode not in ('baseline', 'cutover'):
        raise ValueError('Mode must be baseline or cutover')
    if expected_rate not in (10, 30):
        raise ValueError('Expected rate must be 10 or 30 RPS')
    started, final, plan = read_events(events, run_id)
    source_rows, target_rows = read_rows(source), read_rows(target)
    results = []
    target_only = 0
    for key, request in started.items():
        user_id, trip_name = key
        source_ok, source_detail = validate_rows(source_rows.get(key, []), request, user_id)
        target_ok, target_detail = validate_rows(target_rows.get(key, []), request, user_id)
        same_rows = sorted(tuple(row[field] for field in FIELDS) for row in source_rows.get(key, [])) == \
            sorted(tuple(row[field] for field in FIELDS) for row in target_rows.get(key, []))
        response = final.get(key, {})
        http_confirmed = response.get('outcome') == 'confirmed' and response.get('status') == 201 and \
            isinstance(response['tripId'], int)
        source_present = key in source_rows
        if not source_present and target_ok:
            target_only += 1
        committed = target_ok and (source_ok and same_rows if source_present else mode == 'cutover')
        passed = committed and http_confirmed and str(response['tripId']) == target_detail
        results.append({'userId': user_id, 'tripName': trip_name,
                        'httpStatus': response.get('status'), 'httpOutcome': response.get('outcome', 'interrupted'),
                        'source': source_detail, 'target': target_detail,
                        'sourceTargetEqual': same_rows if source_present else None,
                        'classification': ('confirmed_and_replicated' if source_present else 'confirmed_on_target') if passed else
                        'committed_without_confirmed_http' if committed and not http_confirmed else
                        'not_observed' if source_detail == target_detail == 'not_observed' else 'mismatch',
                        'passed': passed})
    unexpected = sorted((set(source_rows) | set(target_rows)) - set(started))
    report = {'runId': run_id, 'mode': mode, 'started': len(started), 'final': len(final),
              'sourceTrips': len(source_rows), 'targetTrips': len(target_rows),
              'targetOnlyRequests': target_only, 'unexpectedRows': len(unexpected),
              'confirmed': sum(row['passed'] for row in results),
              'committedWithoutConfirmedHttp': sum(row['classification'] == 'committed_without_confirmed_http' for row in results),
              'requests': results}
    report['passed'] = bool(results) and not unexpected and len(final) == len(started) and all(row['passed'] for row in results)
    if summary is not None:
        metrics = json.loads(summary.read_text(encoding='utf-8'))['metrics']
        if not plan or plan.get('mode') != 'load' or plan.get('rate') != expected_rate or plan.get('duration') != 120:
            raise ValueError(f'Expected one {expected_rate} RPS / 120s trip load plan')
        report['plannedRequests'] = plan['rate'] * plan['duration']
        report['summaryStarted'] = metrics.get('trip_create_started', {}).get('values', {}).get('count', 0)
        report['summaryFinalFailures'] = metrics.get('trip_create_final_failures', {}).get('values', {}).get('count', 0)
        report['droppedIterations'] = metrics.get('dropped_iterations', {}).get('values', {}).get('count', 0)
        report['failedThresholds'] = sum(1 for value in metrics.values() for threshold in value.get('thresholds', {}).values()
                                         if threshold.get('ok') is False)
        report['passed'] = report['passed'] and report['plannedRequests'] <= len(started) <= report['plannedRequests'] + 1 and \
            report['summaryStarted'] == len(started) and report['summaryFinalFailures'] == len(started) - report['confirmed'] and \
            report['droppedIterations'] == 0 and report['failedThresholds'] == 0
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', required=True, type=Path)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--target', required=True, type=Path)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--mode', choices=('baseline', 'cutover'), default='baseline')
    parser.add_argument('--summary', type=Path, help='k6 summary.json; required for a load result')
    parser.add_argument('--expected-rate', type=int, choices=(10, 30), default=10,
                        help='Expected load rate; 10 RPS by default for earlier runs')
    parser.add_argument('--require-target-only', action='store_true', help='Require at least one post-switch Target-only trip')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if str(UUID(args.run_id)) != args.run_id:
        raise ValueError('Use a canonical run UUID')
    if args.require_target_only and args.mode != 'cutover':
        raise ValueError('Target-only requirement applies only to cutover mode')
    report = reconcile(args.events, args.source, args.target, args.run_id, args.mode, args.summary,
                       args.expected_rate)
    if args.require_target_only and report['targetOnlyRequests'] == 0:
        report['passed'] = False
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    displayed = {key: value for key, value in report.items() if key != 'requests'} if args.summary else report
    print(json.dumps(displayed, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        raise SystemExit(2)
