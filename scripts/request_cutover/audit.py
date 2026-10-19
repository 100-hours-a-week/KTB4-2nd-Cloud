"""Reconcile sanitized k6 IDs, gate dispatch and persisted trip metadata."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from .config import load
from .control import database


def records(path, source, run):
    for line in Path(path).read_text().splitlines():
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(row, dict) and row.get('source') == source and row.get('run_id') == run:
            yield row


def reconcile(config, k6_path, gate_path):
    k6 = list(records(k6_path, 'request-auth-cutover-k6', config['run_id']))
    gate = list(records(gate_path, 'yeodam-request-cutover', config['run_id']))
    starts = [r for r in k6 if r.get('event') == 'started']
    finals = [r for r in k6 if r.get('event') == 'final']
    ids = Counter(r['request_id'] for r in starts)
    outcomes = Counter(r['request_id'] for r in finals)
    events = defaultdict(Counter)
    for row in gate:
        if row.get('request_id') in ids:
            events[row['request_id']][row['event']] += 1
    problems = []
    if not ids:
        problems.append('no_k6_requests')
    for rid, count in ids.items():
        if count != 1 or outcomes[rid] != 1 or any(events[rid][event] != 1
                for event in ('accepted', 'forwarded', 'completed')):
            problems.append('request_not_reconciled:' + rid)
    if set(outcomes) != set(ids):
        problems.append('unexpected_final_ids')
    if any(not r.get('confirmed') for r in finals):
        problems.append('client_failure_or_unknown')
    writes = [r for r in starts if r['operation'] == 'trip_metadata']
    with database(config) as db:
        with db.cursor() as cursor:
            for row in writes:
                cursor.execute('SELECT trip_id FROM trips WHERE user_id=%s AND trip_name=%s',
                               (row['user_id'], row['trip_name']))
                matches = cursor.fetchall()
                final = [r for r in finals if r['request_id'] == row['request_id']]
                if len(matches) != 1 or len(final) != 1 or matches[0]['trip_id'] != final[0]['trip_id']:
                    problems.append('persisted_trip_mismatch:' + row['request_id'])
    return {'run_id': config['run_id'], 'started': len(starts), 'final': len(finals),
            'writes': len(writes), 'problems': problems, 'reconciled': not problems,
            'boundary': 'trip metadata only; no photo/AI completion claim'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True)
    p.add_argument('--k6', required=True)
    p.add_argument('--gate', required=True)
    args = p.parse_args()
    report = reconcile(load(args.config), args.k6, args.gate)
    print(json.dumps(report, separators=(',', ':')))
    if not report['reconciled']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
