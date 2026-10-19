"""Aggregate local JSONL Nginx logs. No AWS calls, sockets or deployment actions."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re

ROUTES = {'/', '/api/trips', '/api/trips/map', '/api/actuator/health', '/api/auth/csrf',
          '/api/users/me', '/api/trips/:tripId', '/api/trips/:tripId/processing-status',
          '/api/trips/:tripId/initial-attachments', 'other'}


def timestamp(value):
    if isinstance(value, bool):
        raise ValueError('Invalid timestamp')
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    if not isinstance(value, str):
        raise ValueError('Invalid timestamp')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError('Timezone required')
    return result.astimezone(timezone.utc)


def p95(values):
    return sorted(values)[math.ceil(0.95 * len(values)) - 1] if values else None


def rows(path):
    # JSONL only: raw access JSON or one CloudWatch event {timestamp,message}
    # per line. Reject oversized lines, rather than silently dropping evidence.
    with path.open(encoding='utf-8') as stream:
        for index in range(1, 500002):
            line = stream.readline(1048577)
            if not line:
                return
            if index > 500000 or len(line) > 1048576:
                raise ValueError('Local input exceeds row/line bounds')
            if line.strip():
                yield index, line


def summarize(access, error, start, end, error_offset):
    if start >= end:
        raise ValueError('Window must have positive duration')
    groups = defaultdict(lambda: {'count': 0, 'times': []})
    minutes = defaultdict(lambda: {'requests': 0, '401': 0, '429': 0, '499': 0,
                                  '5xx': 0, 'connectionErrors': 0, 'times': []})
    invalid = Counter()
    outside = Counter()
    included = Counter()
    for index, line in rows(access):
        try:
            event = json.loads(line)
            if 'message' in event:
                event = json.loads(event['message'])
            at = timestamp(event['timestamp'])
            status = int(event['status'])
            seconds = float(event['request_time'])
            upstream = str(event['upstream_status'])
            method = str(event['method'])
            if not (100 <= status <= 599 and math.isfinite(seconds) and seconds >= 0):
                raise ValueError('Invalid status/time')
            if not re.fullmatch(r'(?:-|[1-5][0-9]{2})(?:[, :]+(?:-|[1-5][0-9]{2}))*', upstream):
                raise ValueError('Invalid upstream status')
            if method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'):
                raise ValueError('Invalid method')
            route = event.get('normalized_route', 'other')
            route = route if route in ROUTES else 'other'
            limit_req = event.get('limit_req_status', 'not_logged')
            limit_conn = event.get('limit_conn_status', 'not_logged')
            known_limits = ('not_logged', '-', '', 'PASSED', 'DELAYED', 'REJECTED',
                            'DELAYED_DRY_RUN', 'REJECTED_DRY_RUN')
            if limit_req not in known_limits or limit_conn not in known_limits:
                raise ValueError('Unknown limit decision')
        except (ValueError, TypeError, KeyError, OverflowError):
            invalid['access'] += 1
            continue
        if not start <= at < end:
            outside['access'] += 1
            continue
        included['access'] += 1
        group = groups[(method, route, status, upstream, limit_req, limit_conn)]
        group['count'] += 1
        group['times'].append(seconds)
        minute = minutes[at.replace(second=0, microsecond=0).isoformat()]
        minute['requests'] += 1
        minute[str(status)] = minute.get(str(status), 0) + 1
        minute['5xx'] += int(status >= 500)
        minute['times'].append(seconds)
    if error:
        for index, line in rows(error):
            try:
                if line.lstrip().startswith('{'):
                    event = json.loads(line)
                    at = timestamp(event['timestamp'])
                    message = event['message']
                else:
                    if error_offset is None:
                        raise ValueError('Explicit timezone required for native error log')
                    at = datetime.strptime(line[:19], '%Y/%m/%d %H:%M:%S').replace(tzinfo=error_offset).astimezone(timezone.utc)
                    message = line
                if not isinstance(message, str):
                    raise ValueError('Invalid error message')
            except (ValueError, TypeError, KeyError, OverflowError):
                invalid['error'] += 1
                continue
            if not start <= at < end:
                outside['error'] += 1
                continue
            included['error'] += 1
            if 'worker_connections are not enough' in message:
                minutes[at.replace(second=0, microsecond=0).isoformat()]['connectionErrors'] += 1
    route_rows = [{'method': k[0], 'route': k[1], 'status': k[2], 'upstream_status': k[3],
                   'limit_req_status': k[4], 'limit_conn_status': k[5],
                   'count': v['count'], 'p95_nearest_rank_seconds': p95(v['times'])}
                  for k, v in sorted(groups.items())]
    minute_rows = [{'minute_utc': k, **{n: v[n] for n in ('requests', '401', '429', '499', '5xx', 'connectionErrors')},
                    'p95_nearest_rank_seconds': p95(v['times'])} for k, v in sorted(minutes.items())]
    return {'schema': 1, 'complete': not invalid, 'hasAccessData': included['access'] > 0,
            'window': {'startInclusive': start.isoformat(), 'endExclusive': end.isoformat()},
            'included': dict(included), 'outsideWindow': dict(outside), 'invalidLines': dict(invalid),
            'routes': route_rows, 'minutes': minute_rows,
            'limits': ['All traffic in window; source/normal/replay attribution requires separate evidence.',
                       'Error occurrences are not unique failed requests.',
                       'Request latency is not TCP connection lifetime.',
                       'Percentile is nearest-rank, not necessarily the k6 interpolation method.',
                       'No current run, photo preservation or AI completion inferred from log status.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--access', type=Path, required=True)
    parser.add_argument('--error', type=Path)
    parser.add_argument('--error-utc-offset', help='Native Nginx error-log timezone, e.g. +09:00')
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.run_id):
            raise ValueError('Safe run ID required')
        offset = None
        if args.error_utc_offset:
            if not re.fullmatch(r'[+-](?:0[0-9]|1[0-4]):[0-5][0-9]', args.error_utc_offset):
                raise ValueError('Invalid UTC offset')
            sign = 1 if args.error_utc_offset[0] == '+' else -1
            offset = timezone(sign * timedelta(hours=int(args.error_utc_offset[1:3]), minutes=int(args.error_utc_offset[4:])))
        result = summarize(args.access, args.error, timestamp(args.start), timestamp(args.end), offset)
        result['runId'] = args.run_id
        def source_hash(path):
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1048576), b''):
                    digest.update(block)
            return digest.hexdigest()

        result['sourceSha256'] = {key: source_hash(path)
                                  for key, path in [('access', args.access), ('error', args.error)] if path}
        args.output.mkdir(exist_ok=False)
        (args.output / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
        for key in ('routes', 'minutes'):
            entries = result[key]
            with (args.output / f'{key}.csv').open('w', newline='') as stream:
                fields = list(entries[0]) if entries else (['method', 'route', 'status', 'upstream_status', 'limit_req_status', 'limit_conn_status', 'count', 'p95_nearest_rank_seconds'] if key == 'routes' else ['minute_utc', 'requests', '401', '429', '499', '5xx', 'connectionErrors', 'p95_nearest_rank_seconds'])
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(entries)
        print(json.dumps({'output': str(args.output), 'complete': result['complete'], 'invalidLines': result['invalidLines']}))
        return 0 if result['complete'] else 1
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
