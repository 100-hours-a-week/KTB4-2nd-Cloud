#!/usr/bin/env python3
"""Print bounded, secret-free aggregates from one trip diagnostic window."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import statistics


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def stats(values):
    if not values:
        return None
    return {'average': round(statistics.mean(values), 2),
            'p95': round(percentile(values, 0.95), 2),
            'maximum': round(max(values), 2)}


def records(path, start, end):
    with path.open() as stream:
        for line in stream:
            row = json.loads(line)
            moment = datetime.fromisoformat(row['time'])
            if start <= moment <= end:
                yield row


def tab_map(raw):
    return dict(line.split('\t', 1) for line in raw.splitlines() if '\t' in line)


def summarize(cpu_path, db_path, start, end):
    cpu = [row for row in records(cpu_path, start, end) if row['event'] == 'cpu']
    grouped = {}
    for row in cpu:
        for name, metrics in row['groups'].items():
            grouped.setdefault(name, []).append(metrics['cpuPctOfOneCore'])
    result = {'cpuSamples': len(cpu),
              'hostCpuPct': stats([row['hostCpuPct'] for row in cpu if row['hostCpuPct'] is not None]),
              'processCpuPctOfOneCore': {name: stats(values) for name, values in sorted(grouped.items())}}
    if db_path is None:
        return result
    db_rows = list(records(db_path, start, end))
    db = [row for row in db_rows if row['event'] == 'db']
    result['dbSamples'] = len(db)
    result['dbErrors'] = sum(row['event'] == 'db_error' for row in db_rows)
    if len(db) < 2:
        return result
    source = [tab_map(row['sourceStatus']) for row in db]
    keys = ('Questions', 'Com_commit', 'Com_insert', 'Innodb_row_lock_waits',
            'Innodb_row_lock_time', 'Innodb_log_waits', 'Innodb_data_fsyncs')
    result['sourceStatusDelta'] = {key: int(source[-1].get(key, 0)) - int(source[0].get(key, 0))
                                   for key in keys}
    result['threadsRunningMax'] = max(int(row.get('Threads_running', 0)) for row in source)
    pool = []
    for row in db:
        for line in row['proxyPool'].splitlines():
            fields = line.split('\t')
            if len(fields) == 4:
                pool.append([int(field) for field in fields])
    result['proxyConnUsedMax'] = max((row[0] for row in pool), default=None)
    result['proxyConnErrMax'] = max((row[2] for row in pool), default=None)
    digests = []
    for row in (db[0], db[-1]):
        values = {}
        for line in row['proxyDigest'].splitlines():
            fields = line.split('\t')
            if len(fields) == 4:
                values[fields[0]] = [int(field) for field in fields[1:]]
        digests.append(values)
    changes = []
    for digest, last in digests[-1].items():
        first = digests[0].get(digest, [0, 0, 0])
        count = max(0, last[0] - first[0])
        if count:
            changes.append({'digest': digest, 'countDelta': count,
                            'sumTimeDeltaUs': max(0, last[1] - first[1]),
                            'maxTimeUs': last[2]})
    result['topDigestDeltas'] = sorted(changes, key=lambda row: row['sumTimeDeltaUs'], reverse=True)[:10]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cpu-file', type=Path, required=True)
    parser.add_argument('--db-file', type=Path)
    parser.add_argument('--start', type=datetime.fromisoformat, required=True)
    parser.add_argument('--end', type=datetime.fromisoformat, required=True)
    args = parser.parse_args()
    if not args.start.tzinfo or not args.end.tzinfo or args.start >= args.end:
        parser.error('start and end must be ordered timezone-aware ISO timestamps')
    print(json.dumps(summarize(args.cpu_file, args.db_file, args.start, args.end), sort_keys=True))


if __name__ == '__main__':
    main()
