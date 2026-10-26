#!/usr/bin/env python3
"""Offline #430/#433 verdict/commit reconciliation. No network or policy mutation."""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location('create_analysis', Path(__file__).with_name('analyze-nginx-create.py'))
common = importlib.util.module_from_spec(spec); spec.loader.exec_module(common)


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--evidence', required=True, type=Path)
    root = p.parse_args().evidence
    capture = root/'capture'
    captures = sorted(root.glob('capture*/app'))
    access = [x for c in captures for x in common.records(c/'access.jsonl')]
    loki = [x['data'] for c in captures for x in common.records(c.parent/'plg/loki-access.jsonl')]
    counts = Counter(x['request_id'] for x in access); found = Counter(x['request_id'] for x in loki)
    byid = {x['request_id']: x for x in access}
    audits = [json.loads((c/'journey-audit.json').read_text()) for c in captures if (c/'journey-audit.json').exists()]
    audit = max(audits, key=lambda x: x['utc'])
    samples = sorted([x for c in captures for x in common.records(c/'samples.jsonl')], key=lambda x:x['utc'])
    stages = []
    for folder in sorted((root/'generator').iterdir()):
        if not folder.is_dir():
            continue
        result = json.loads((folder/'result.json').read_text()); events = common.records(folder/'events.jsonl')
        prefix = 'c'+hashlib.sha256(result['runId'].encode()).hexdigest()[:5]
        rows = [x for x in audit['trips'] if x['tripName'].startswith(prefix)]
        finals = [x for x in events if x.get('event') == 'final']
        normal = [x for x in events if x.get('event') == 'normal']
        ids = {x['requestId'] for x in finals if x.get('requestId')}
        selected = [byid[x] for x in ids if x in byid]
        normal_rows = [byid[x['requestId']] for x in normal if x.get('requestId') in byid]
        rejected = [x for x in finals if x['outcome'] == 'rejected']
        rejected_keys = {(x['userId'], x['tripName']) for x in rejected}
        server_rejected = [byid[x['requestId']] for x in rejected if x.get('requestId') in byid]
        start, end = common.milliseconds(result['startedAt']), common.milliseconds(result['endedAt'])
        timed = [x for x in samples if start <= common.milliseconds(x['utc']) <= end]
        peers = {x['sourceId'] for x in selected}
        window = [x for x in access if x['sourceId'] in peers and x['method'] == 'POST'
                  and x['normalized_route'] == '/api/trips' and start <= float(x['msec'])*1000 <= end]
        def latency(outcome):
            values = [x['elapsedMs'] for x in finals if x['outcome'] == outcome]
            return {'count': len(values), 'p95': common.quantile(values, .95), 'max': max(values, default=None)}
        container_names = {v['name'] for x in timed for v in x.get('containers', [])}
        stages.append({**result, 'reconciliation': common.reconcile(events, rows, access),
            'createVerdicts': dict(Counter(x.get('limit_req_status', 'MISSING') for x in selected)),
            'samePeerWindowVerdicts': dict(Counter(x.get('limit_req_status', 'MISSING') for x in window)),
            'normalVerdicts': dict(Counter(x.get('limit_req_status', 'MISSING') for x in normal_rows)),
            'normalStatus': dict(Counter(x['status'] for x in normal)),
            'rejected': {'events': len(rejected), 'linkedServerEvents': len(server_rejected),
                'unlinkedServerEvents': len(rejected)-len(server_rejected),
                'dbRows': sum((x['userId'], x['tripName']) in rejected_keys for x in rows),
                'upstreamConnected': sum(x.get('upstream_status') not in ('', '-') for x in server_rejected),
                'wrongVerdict': sum(x.get('limit_req_status') != 'REJECTED' or x['status'] != 429 for x in server_rejected)},
            'confirmedLatencyMs': latency('confirmed'), 'rejectedLatencyMs': latency('rejected'),
            'resources': {'samples': len(timed), 'cpuMax': max((x.get('cpuPercent') or 0 for x in timed), default=None),
                'cpuMedian': common.quantile([x['cpuPercent'] for x in timed if x.get('cpuPercent') is not None], .5),
                'containerMaxOneCore': {n: max((v['cpuPercentOneCore'] for x in timed for v in x.get('containers', [])
                     if v['name'] == n and v.get('cpuPercentOneCore') is not None), default=None) for n in container_names}},
            'metrics': json.loads((folder/'summary.json').read_text())['metrics']})
    ui = []
    unmatched = []
    client_failures = []
    for file in sorted((root/'client').glob('p*-events.json')):
        events = json.loads(file.read_text())
        for e in events:
            if e.get('event') != 'end':
                continue
            if not e.get('requestId'):
                client_failures.append({'tab': file.stem.split('-')[0], 'path': e['path'], 'status': e['status']})
                continue
            row = byid.get(e['requestId'])
            if not row:
                unmatched.append(e['requestId'])
            if row:
                ui.append({'tab': file.stem.split('-')[0], 'method': e['method'], 'path': e['path'],
                    'status': e['status'], 'requestId': e['requestId'], 'tripId': e.get('tripId'),
                    'elapsedMs': e['endMs']-e['atMs'], 'limit': row.get('limit_req_status', 'MISSING'),
                    'sourceId': row['sourceId'], 'msec': row['msec']})
    create = [x for x in ui if x['method'] == 'POST' and x['path'] == '/api/trips']
    photos = [x for x in ui if x['method'] == 'POST' and x['path'].endswith('/initial-attachments')]
    report = {'stages': stages, 'ui': {'creates': create, 'serverPhotoPosts': photos,
        'allMatchedRequests': len(ui), 'unmatchedRequestIds': unmatched, 'noRequestIdClientEvents': client_failures,
        'statuses': dict(Counter(x['status'] for x in ui)),
        'trips': audit['uiTrips']},
        'requestIds': {'app': sum(counts.values()), 'loki': sum(found.values()),
            'missing': sum((counts-found).values()), 'extra': sum((found-counts).values())},
        'collection': json.loads((capture/'app/result.json').read_text()),
        'collections': [json.loads((c/'result.json').read_text()) for c in captures],
        'samples': len(samples),
        'allRetainedOwnedTrips': len(audit['trips']),
        'limits': ['Dry Run/enforcement are separate runs; verify outcome and upstream rather than assuming429 implies no commit',
            'one browser peer / three synthetic owners behind one generator peer, not all NAT sizes',
            'photo before-send retry, not metadata-response loss or mid-batch recovery']}
    (root/'analysis.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({'stages': [{k: x[k] for k in ['runId', 'rate', 'mode', 'exitCode', 'createVerdicts', 'normalStatus']} for x in stages],
        'uiCreate': len(create), 'uiPhotos': len(photos), 'requestIds': report['requestIds'], 'samples': report['samples']}))


if __name__ == '__main__':
    main()
