#!/usr/bin/env python3
"""Offline #428 stage, Nginx request-ID and retained trip-row reconciliation."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path


def records(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def milliseconds(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()*1000


def quantile(values, percentile):
    if not values:
        return None
    a = sorted(values); at = (len(a)-1)*percentile
    lo = int(at); hi = min(lo+1, len(a)-1)
    return a[lo]+(a[hi]-a[lo])*(at-lo)


def reconcile(events, rows, access):
    starts = [x for x in events if x.get('event') == 'started']
    finals = [x for x in events if x.get('event') == 'final']
    lookups = Counter((int(x['userId']), x['tripName']) for x in rows)
    access_ids = Counter(x.get('request_id') for x in access)
    trip_ids = {(int(r['tripId']), int(r['userId']), r['tripName']) for r in rows}
    successes = [x for x in finals if x['outcome'] == 'confirmed']
    confirmed_keys = {(x['userId'], x['tripName']) for x in successes}
    requested_keys = {(x['userId'], x['tripName']) for x in starts}
    return {'startedEvents': len(starts), 'finalEvents': len(finals), 'confirmedEvents': len(successes),
        'startedWithoutFinal': len(requested_keys-{(x['userId'], x['tripName']) for x in finals}),
        'duplicateStartEvents': len(starts)-len(requested_keys),
        'duplicateFinalEvents': len(finals)-len({(x['userId'], x['tripName']) for x in finals}),
        'confirmedMissingDb': sum(lookups[x] == 0 for x in confirmed_keys),
        'dbRowsForStarted': sum(lookups[x] for x in requested_keys),
        'committedWithoutConfirmedResponse': sum(lookups[x] for x in requested_keys-confirmed_keys),
        'duplicateRowsForStarted': sum(max(0, lookups[x]-1) for x in requested_keys),
        'confirmedIdMismatch': sum((int(x['tripId']), x['userId'], x['tripName']) not in trip_ids for x in successes),
        'finalsWithRequestId': sum(bool(x.get('requestId')) for x in finals),
        'finalsWithoutRequestId': sum(not x.get('requestId') for x in finals),
        'finalsWithIdMissingOrDuplicateAccess': sum(access_ids[x['requestId']] != 1 for x in finals if x.get('requestId'))}


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--evidence', required=True, type=Path)
    a = p.parse_args(); root = a.evidence
    captures = sorted(root.glob('capture-*/app'))
    access = [x for c in captures for x in records(c/'access.jsonl')]
    samples = [x for c in captures for x in records(c/'samples.jsonl')]
    audits = [json.loads((c/'journey-audit.json').read_text()) for c in captures if (c/'journey-audit.json').exists()]
    if not audits:
        raise ValueError('A retained-row audit is required')
    audit = max(audits, key=lambda x: x['utc'])
    rows = audit['trips']; output = []
    for folder in sorted((root/'generator').iterdir()):
        if not folder.is_dir():
            continue
        plan = json.loads((folder/'result.json').read_text())
        summary = json.loads((folder/'summary.json').read_text())['metrics']
        events = records(folder/'events.jsonl')
        start, end = milliseconds(plan['startedAt']), milliseconds(plan['endedAt'])
        timed = [x for x in samples if start <= milliseconds(x['utc']) <= end]
        prefix = 'c'+hashlib.sha256(plan['runId'].encode()).hexdigest()[:5]
        stage_rows = [x for x in rows if x['tripName'].startswith(prefix)]
        normal = [x for x in events if x.get('event') == 'normal']
        result = {**plan, 'reconciliation': reconcile(events, stage_rows, access),
            'stageDbRows': len(stage_rows), 'regionRows': sum(x['regionRows'] for x in stage_rows),
            'create': summary.get('create_elapsed_ms', {}).get('values'),
            'normalRead': summary.get('normal_read_elapsed_ms', {}).get('values'),
            'normalRequests': len(normal), 'normalHttpFailures': sum(x['status'] not in [0, 200] for x in normal),
            'normalZeroStatusOnAbort': sum(x['status'] == 0 for x in normal),
            'normalOverOneSecond': sum(x['elapsedMs'] > 1000 for x in normal),
            'dropped': summary.get('dropped_iterations', {}).get('values', {}).get('count'),
            'appSamples': len(timed), 'appSampleErrors': sum('errorType' in x for x in timed),
            'appCpuMax': max((x['cpuPercent'] for x in timed if x.get('cpuPercent') is not None), default=None),
            'appCpuMedian': quantile([x['cpuPercent'] for x in timed if x.get('cpuPercent') is not None], .5),
            'nginxFdMax': max((x['nginxFdTotal'] for x in timed if x.get('nginxFdTotal') is not None), default=None),
            'httpsEstablishedMax': max((x['httpsEstablished'] for x in timed if x.get('httpsEstablished') is not None), default=None),
            'processCpuMaxOneCore': {name: max([v['cpuPercentOneCore'] for x in timed
                for v in x.get('processes', []) if v['name'] == name and v.get('cpuPercentOneCore') is not None], default=None) for name in ['java', 'mysqld', 'nginx', 'alloy']},
            'mysqlSamples': [x['mysqlStatus'] for x in timed if 'mysqlStatus' in x]}
        starts = sorted(x['atMs'] for x in events if x.get('event') == 'started')
        changes = [(x['atMs'], 1 if x['event'] == 'started' else -1) for x in events if x.get('event') in ['started', 'final']]
        active = peak = 0
        for at, delta in sorted(changes, key=lambda x: (x[0], x[1])):
            active += delta; peak = max(peak, active)
        def window_count(ms):
            left = maximum = 0
            for right, at in enumerate(starts):
                while starts[left] <= at-ms:
                    left += 1
                maximum = max(maximum, right-left+1)
            return maximum
        final_ids = {x['requestId'] for x in events if x.get('event') == 'final' and x.get('requestId')}
        server = [x for x in access if x.get('request_id') in final_ids]
        result['clientOutstandingPeak'] = peak
        result['slidingStarts'] = {str(ms): window_count(ms) for ms in [1000, 10000, 60000]}
        result['confirmedNginxConnections'] = len({x.get('connection') for x in server})
        result['confirmedProtocols'] = dict(Counter(x.get('http_protocol') for x in server))
        result['confirmedUpstreamSeconds'] = {'max': max((float(x['upstream_response_time']) for x in server), default=None),
            'p95': quantile([float(x['upstream_response_time']) for x in server], .95)}
        gen = records(folder/'generator.jsonl'); total_ticks = 0; interval_cpu = []
        for prev, cur in zip(gen, gen[1:]):
            before = {(x['pid'], x['startTicks']): x['cpuTicks'] for x in prev['k6']}
            ticks = 0
            for x in cur['k6']:
                key = (x['pid'], x['startTicks'])
                if key in before:
                    ticks += max(0, x['cpuTicks']-before[key])
            total_ticks += ticks
            seconds = cur['elapsedSeconds']-prev['elapsedSeconds']
            if seconds > 0:
                interval_cpu.append(100*ticks/cur['clockTicks']/seconds)
        duration = gen[-1]['elapsedSeconds']-gen[0]['elapsedSeconds'] if len(gen) > 1 else 0
        result['generator'] = {'averageCpuOneCore': 100*total_ticks/gen[0]['clockTicks']/duration if duration else None,
            'maxSampleCpuOneCore': max(interval_cpu, default=None),
            'maxRssMiB': max((sum(x['rssPages'] for x in s['k6'])*s['pageBytes']/1024**2 for s in gen), default=None),
            'logicalCpus': gen[0]['logicalCpus'] if gen else None}
        output.append(result)
    report = {'stages': output, 'captureIndexes': [json.loads((c/'index.json').read_text()) for c in captures],
        'totalRetainedTrips': len(rows), 'audit': {k: v for k, v in audit.items() if k != 'trips'},
        'auditSnapshots': [{'utc': x['utc'], 'trips': len(x['trips'])} for x in audits],
        'limits': ['single sequential run per rate, three fresh owners, growing retained data',
                   'no database/pool profiler; not a global capacity or service SLO',
                   'CPU100%=one core for processes; hostCPU has a separate denominator']}
    (root/'analysis.json').write_text(json.dumps(report, indent=2))
    for r in sorted(output, key=lambda x: x['startedAt']):
        print(json.dumps({k: r[k] for k in ['runId', 'mode', 'rate', 'exitCode', 'create', 'normalRequests',
            'normalHttpFailures', 'normalOverOneSecond', 'dropped', 'reconciliation', 'appCpuMedian', 'appCpuMax', 'generator']}))


if __name__ == '__main__':
    main()
