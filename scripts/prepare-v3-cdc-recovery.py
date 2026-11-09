#!/usr/bin/env python3
"""Plan a guarded one-time empty-Outbox recovery. No live clients or reset API."""
import argparse
import json
from pathlib import Path
from v3_outbox_contract import CONFIG, CONTRACT, require

NAME = CONTRACT['connectorName']
RETRIES = {'enabled': True, 'maxRestarts': 3}
NORMAL_CONFIG = {k: v for k, v in CONFIG.items() if k not in ['connector.class', 'tasks.max']}


def plan(connector, counts=None, offsets=None, restore=False):
    meta, spec = connector['metadata'], connector['spec']
    require(meta.get('name') == NAME and meta.get('namespace') == 'kafka', 'Only the V3 Outbox Connector may be recovered')
    require(isinstance(meta.get('resourceVersion'), str) and meta['resourceVersion'], 'Fresh resourceVersion required')
    expected = dict(NORMAL_CONFIG)
    if restore:
        expected['snapshot.mode'] = 'when_needed'
    require(spec.get('class') == 'io.debezium.connector.mysql.MySqlConnector' and spec.get('tasksMax') == 1
            and spec.get('state') == 'running' and spec.get('config') == expected, 'Unexpected source/filter/identity configuration')
    status = connector.get('status', {})
    tasks = status.get('connectorStatus', {}).get('tasks', [])
    require(len(tasks) == 1 and tasks[0].get('id') == 0, 'Exactly Task 0 required')
    if restore:
        require(spec.get('autoRestart') == {'enabled': False}, 'Recovery must keep automatic retries disabled')
        require(status.get('observedGeneration') == meta.get('generation') and any(
            c.get('type') == 'Ready' and c.get('status') == 'True' for c in status.get('conditions', []))
            and tasks[0].get('state') == 'RUNNING', 'Recovery Task must be freshly Ready before restore')
        entries = (offsets or {}).get('offsets', [])
        require(len(entries) == 1 and entries[0].get('partition') == {'server': 'v3.yeodam.outbox'}, 'One V3 source offset required')
        offset = entries[0].get('offset', {})
        require(isinstance(offset.get('file'), str) and offset['file'].startswith('mysql-bin.')
                and type(offset.get('pos')) is int and offset['pos'] > 0
                and offset.get('snapshot') in (None, False, 'false')
                and offset.get('snapshot_completed') is not False, 'Committed non-snapshot binlog offset required')
    else:
        require(spec.get('autoRestart') in ({'enabled': False}, RETRIES), 'Unexpected retry policy')
        require(isinstance(counts, dict) and set(counts) == set(CONTRACT['tables'])
                and all(type(n) is int and n == 0 for n in counts.values()), 'All four Outbox tables must be empty; manual review required otherwise')
        trace = tasks[0].get('trace', '').lower()
        require(tasks[0].get('state') == 'FAILED' and 'binlog' in trace and 'no longer available' in trace,
                'Only the diagnosed missing-binlog failure may use snapshot recovery')
    mode = 'when_needed' if not restore else 'initial'
    retries = {'enabled': False} if not restore else RETRIES
    # Compare-and-swap prevents applying a stale review after concurrent updates.
    return [
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': meta['resourceVersion']},
        {'op': 'test', 'path': '/spec/config', 'value': expected},
        {'op': 'test', 'path': '/spec/autoRestart', 'value': spec['autoRestart']},
        {'op': 'replace', 'path': '/spec/config/snapshot.mode', 'value': mode},
        {'op': 'replace', 'path': '/spec/autoRestart', 'value': retries},
    ]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--connector', required=True, type=Path)
    p.add_argument('--counts', type=Path, help='Fresh table-name to total-row-count map; no row payloads')
    p.add_argument('--offsets', type=Path, help='Read-only Connect source offset metadata for restore')
    p.add_argument('--restore', action='store_true')
    p.add_argument('--output', required=True, type=Path)
    a = p.parse_args()
    patch = plan(json.loads(a.connector.read_text()),
                 json.loads(a.counts.read_text()) if a.counts else None,
                 json.loads(a.offsets.read_text()) if a.offsets else None, a.restore)
    with a.output.open('x') as f:
        json.dump(patch, f, indent=2)
    print('Prepared V3-only CAS patch; no resources accessed or offsets reset. Review and apply separately.')


if __name__ == '__main__':
    main()
