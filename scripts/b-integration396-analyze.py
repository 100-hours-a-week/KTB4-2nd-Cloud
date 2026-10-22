#!/usr/bin/env python3
"""Read retained completion timing only; never connect to AWS or modify a Run."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path


def percentiles(values):
    ordered = sorted(values)
    if not ordered:
        return {}
    return {'count': len(ordered), 'mean': sum(ordered)/len(ordered),
            **{label: ordered[int((len(ordered) - 1) * quantile)]
               for label, quantile in [('p50', .5), ('p95', .95), ('p99', .99), ('max', 1)]}}


def analyze(completions, started_at, expected_count, window_seconds=60):
    if not math.isfinite(started_at) or expected_count < 1 or window_seconds < 1:
        raise ValueError('Invalid analysis contract')
    # Output never copies arbitrary input fields (SQL, parameters or tokens).
    rows = []
    for entry in completions.values():
        row = {key: entry[key] for key in ('origin', 'partition', 'queuedAtMs',
               'consumerStartedAtMs', 'committedAtMs', 'applyElapsedUs')}
        if row['origin'] not in ('source', 'target') or row['partition'] not in range(8):
            raise ValueError('Unexpected origin/partition')
        for key in ('queuedAtMs', 'consumerStartedAtMs', 'committedAtMs', 'applyElapsedUs'):
            if not isinstance(row[key], (int, float)) or not math.isfinite(row[key]):
                raise ValueError('Missing or invalid timing')
        if not 0 < row['queuedAtMs'] <= row['consumerStartedAtMs'] <= row['committedAtMs']:
            raise ValueError('Clock ordering invalid; do not classify as queue time')
        if row['applyElapsedUs'] < 0:
            raise ValueError('Invalid apply duration')
        diagnostic = entry.get('diagnostic', {})
        allowed = {'received_ms', 'scheduler_wait_us', 'tx_entry_us'}
        for category in ('connection','journal_read','trip_lookup','trip_write','region_write','journal_write','other_sql','commit','rollback'):
            allowed.update((category+'_us', category+'_count'))
        row['diagnostic'] = {k: v for k, v in diagnostic.items() if k in allowed and isinstance(v, int) and v >= 0}
        rows.append(row)
    if not rows:
        raise ValueError('No completion observations')
    start_ms = started_at * 1000
    final_seconds = max(r['committedAtMs'] - start_ms for r in rows) / 1000
    if min(r['queuedAtMs'] for r in rows) < start_ms:
        raise ValueError('Completion file contains requests before this Run')
    bins = []
    for left in range(0, int(max(0, final_seconds)) + 1, window_seconds):
        right = left + window_seconds
        a, b = start_ms + left * 1000, start_ms + right * 1000
        item = {'fromSeconds': left, 'toSeconds': right,
                'createdObserved': sum(a <= r['queuedAtMs'] < b for r in rows),
                'observedPendingAtEnd': sum(r['queuedAtMs'] < b <= r['committedAtMs'] for r in rows)}
        for origin in ('source', 'target'):
            role_rows = [r for r in rows if r['origin'] == origin]
            done = [r for r in role_rows if a <= r['committedAtMs'] < b]
            # Wall-clock occupancy includes connection/SQL/lock waits, not CPU use.
            occupied_ms = sum(max(0, min(b, r['committedAtMs']) -
                                     max(a, r['consumerStartedAtMs'])) for r in role_rows)
            item[origin] = {'completed': len(done), 'completedPerSecond': len(done) / window_seconds,
                'meanActiveObserved': occupied_ms / (window_seconds * 1000),
                'completionMs': percentiles([r['committedAtMs'] - r['queuedAtMs'] for r in done]),
                'queueMs': percentiles([r['consumerStartedAtMs'] - r['queuedAtMs'] for r in done]),
                'applyUs': percentiles([r['applyElapsedUs'] for r in done]),
                'completedByPartition': dict(Counter(r['partition'] for r in done))}
        bins.append(item)
    boundary = percentiles([r['committedAtMs'] - r['queuedAtMs'] for r in rows])['p95']
    tail = [r for r in rows if r['committedAtMs'] - r['queuedAtMs'] >= boundary]
    complete = len(rows) == expected_count
    diagnostics = {}
    for role in ('source', 'target'):
        rs = [r for r in rows if r['origin'] == role]
        measured = [r for r in rs if 'received_ms' in r['diagnostic']]
        diagnostics[role] = {'observations':len(rs), 'instrumented':len(measured),
            'receivedBeforeSchedulerMs':percentiles([r['diagnostic']['received_ms']-r['queuedAtMs'] for r in measured]),
            'fields':{k:percentiles([r['diagnostic'][k] for r in measured if k in r['diagnostic']])
                      for k in sorted({k for r in measured for k in r['diagnostic'] if k!='received_ms'})},
            'byPartition':{str(p):{'instrumented':sum(r['partition']==p for r in measured),
                'beforeSchedulerMs':percentiles([r['diagnostic']['received_ms']-r['queuedAtMs'] for r in measured if r['partition']==p]),
                'schedulerWaitUs':percentiles([r['diagnostic'].get('scheduler_wait_us',0) for r in measured if r['partition']==p])} for p in range(8)}}
    return {'diagnostics':diagnostics, 'scope': 'completed request timings only', 'observations': len(rows),
            'expectedRequests': expected_count, 'allExpectedCompleted': complete,
            'pendingScope': 'all requests' if complete else 'completed observations only; excludes unapplied requests',
            'completionMs': percentiles([r['committedAtMs'] - r['queuedAtMs'] for r in rows]),
            'p95TailByPartition': dict(Counter(r['partition'] for r in tail)), 'bins': bins}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--completions', type=Path, required=True)
    parser.add_argument('--started-at', type=float, required=True)
    parser.add_argument('--expected-requests', type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(analyze(json.loads(args.completions.read_text()), args.started_at,
                             args.expected_requests), separators=(',', ':')))


if __name__ == '__main__':
    main()
