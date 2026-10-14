#!/usr/bin/env python3
"""Compare complete k6 logical request records with streamed MySQL run exports."""
import argparse
from contextlib import ExitStack
import gzip
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from uuid import UUID

def text_stream(path):
    return gzip.open(path, 'rt') if str(path).endswith('.gz') else open(path, encoding='utf-8')

def identity(run, request, payload, expected_run):
    if run != expected_run or str(UUID(request)) != request or not re.fullmatch('[0-9a-f]{64}', payload):
        raise ValueError('Invalid request identity or mixed run IDs')

def reconcile(events, target, run_id, summary, source=None):
    with tempfile.TemporaryDirectory(prefix='yeodam-reconcile-') as directory, ExitStack() as cleanup:
        db = sqlite3.connect(str(Path(directory) / 'rows.sqlite'))
        cleanup.callback(db.close)
        db.executescript('CREATE TABLE requests(id TEXT PRIMARY KEY, hash TEXT, outcome TEXT); CREATE TABLE target(id TEXT PRIMARY KEY, hash TEXT); CREATE TABLE source(id TEXT PRIMARY KEY, hash TEXT);')
        plans = {}
        starts_by_generator = {}
        for path in events:
            with text_stream(path) as stream:
                for line in stream:
                    # Accept raw console records and k6's timestamp/level prefix; no credential logs are consumed.
                    offset = line.find('{')
                    if offset < 0:
                        if line.strip(): raise ValueError('Non-JSON line in event log; input may be incomplete')
                        continue
                    event = json.loads(line[offset:])
                    if event.get('source') != 'migration-probe': raise ValueError('Unexpected event source')
                    if event.get('runId') != run_id: raise ValueError('Event file contains another run')
                    if event['event'] == 'run_started':
                        if event['generatorId'] in plans: raise ValueError('Duplicate generator ID for this run')
                        plans[event['generatorId']] = (1 if event['mode'] == 'contract' else event['rate'] * event['duration'], event['mode'])
                        starts_by_generator[event['generatorId']] = 0
                        continue
                    if event['event'] not in ('started', 'final'): continue
                    identity(event['runId'], event['requestId'], event['payloadHash'], run_id)
                    request = event['requestId']
                    if event['event'] == 'started':
                        generator = event.get('generatorId')
                        if generator is None and len(plans) == 1:
                            generator = next(iter(plans))  # Older single-generator logs.
                        if generator not in plans:
                            raise ValueError('Started event has no known generator')
                        starts_by_generator[generator] += 1
                        db.execute('INSERT INTO requests VALUES(?,?,NULL)', (request, event['payloadHash']))
                    else:
                        if event['outcome'] not in ('confirmed', 'unconfirmed', 'rejected', 'invalid_response'):
                            raise ValueError('Unknown final outcome')
                        row = db.execute('SELECT hash,outcome FROM requests WHERE id=?', (request,)).fetchone()
                        if not row or row[0] != event['payloadHash'] or row[1] is not None:
                            raise ValueError('Final has no matching unique started event')
                        db.execute('UPDATE requests SET outcome=? WHERE id=?', (event['outcome'], request))
        for table, path in [('target', target), ('source', source)]:
            if not path: continue
            with text_stream(path) as stream:
                for line in stream:
                    fields = line.rstrip('\n').split('\t')
                    if len(fields) != 4: raise ValueError('DB export must have four TSV columns')
                    run, request, payload, _created = fields
                    identity(run, request, payload, run_id)
                    db.execute(f'INSERT INTO {table} VALUES(?,?)', (request, payload))
        def count(sql): return db.execute(sql).fetchone()[0]
        results = {
            'runId': run_id,
            'started': count('SELECT COUNT(*) FROM requests'),
            'confirmed': count("SELECT COUNT(*) FROM requests WHERE outcome='confirmed'"),
            'interrupted': count('SELECT COUNT(*) FROM requests WHERE outcome IS NULL'),
            'finalFailures': count("SELECT COUNT(*) FROM requests WHERE outcome IS NOT NULL AND outcome!='confirmed'"),
            'targetRows': count('SELECT COUNT(*) FROM target'),
            'confirmedMissing': count("SELECT COUNT(*) FROM requests r LEFT JOIN target t ON r.id=t.id WHERE r.outcome='confirmed' AND t.id IS NULL"),
            'unconfirmedButStored': count("SELECT COUNT(*) FROM requests r JOIN target t ON r.id=t.id WHERE r.outcome='unconfirmed' OR r.outcome IS NULL"),
            'rejectedButStored': count("SELECT COUNT(*) FROM requests r JOIN target t ON r.id=t.id WHERE r.outcome IN ('rejected','invalid_response')"),
            'hashMismatch': count('SELECT COUNT(*) FROM requests r JOIN target t ON r.id=t.id WHERE r.hash!=t.hash'),
            'unexpectedTarget': count('SELECT COUNT(*) FROM target t LEFT JOIN requests r ON r.id=t.id WHERE r.id IS NULL'),
        }
        if source:
            results['sourceMissingAtTarget'] = count('SELECT COUNT(*) FROM source s LEFT JOIN target t ON s.id=t.id WHERE t.id IS NULL')
            results['sourceTargetHashMismatch'] = count('SELECT COUNT(*) FROM source s JOIN target t ON s.id=t.id WHERE s.hash!=t.hash')
        summaries = summary if isinstance(summary, list) else [summary]
        metrics = [json.loads(Path(path).read_text())['metrics'] for path in summaries]
        def metric(name): return sum(m.get(name, {}).get('values', {}).get('count', 0) for m in metrics)
        results['plannedRequests'] = sum(value[0] for value in plans.values())
        results['summaryStarted'] = metric('probe_started')
        results['summaryConfirmed'] = metric('probe_confirmed')
        results['summaryFinalFailures'] = metric('probe_final_failures')
        results['firstAttemptFailures'] = metric('probe_first_failures')
        results['retryAttempts'] = metric('probe_retries')
        results['droppedIterations'] = metric('dropped_iterations')
        results['failedThresholds'] = sum(1 for m in metrics for value in m.values()
                                         for threshold in value.get('thresholds', {}).values() if threshold.get('ok') is False)
        results['completeEventLog'] = (results['started'] == results['summaryStarted'] and
                                       results['confirmed'] == results['summaryConfirmed'] and
                                       results['finalFailures'] == results['summaryFinalFailures'])
        # k6 arrival timers and duration cancellation can race at the final
        # boundary. Permit at most one EXTRA iteration per load generator;
        # never excuse under-delivery, dropped iterations or missing records.
        results['generatorLoad'] = {
            name: {'planned': planned, 'started': starts_by_generator[name],
                   'allowedExtra': int(mode == 'load')}
            for name, (planned, mode) in plans.items()
        }
        results['plannedLoadCompleted'] = bool(plans) and len(plans) == len(summaries) and all(
            value['planned'] <= value['started'] <= value['planned'] + value['allowedExtra']
            for value in results['generatorLoad'].values())
        results['passed'] = results['completeEventLog'] and results['plannedLoadCompleted'] and all(results[key] == 0 for key in (
            'interrupted', 'finalFailures', 'confirmedMissing', 'hashMismatch', 'unexpectedTarget', 'droppedIterations', 'failedThresholds'))
        if source:
            results['passed'] = results['passed'] and results['sourceMissingAtTarget'] == 0 and results['sourceTargetHashMismatch'] == 0
        return results

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', required=True, nargs='+', type=Path)
    parser.add_argument('--target', required=True, type=Path)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--summary', required=True, nargs='+', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if str(UUID(args.run_id)) != args.run_id: raise ValueError('Use a canonical run UUID')
    report = reconcile(args.events, args.target, args.run_id, args.summary, args.source)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1

if __name__ == '__main__':
    try: raise SystemExit(main())
    except (ValueError, OSError, KeyError, sqlite3.IntegrityError) as error:
        print(f'ERROR: {error}', file=sys.stderr); raise SystemExit(2)
