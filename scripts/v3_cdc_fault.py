"""Bounded CDC fault preparation and evidence. No AWS or Secret-value clients.

NetworkChaos externalTargets pins MySQL IPv4 addresses AND port 3306. Only the
Connect network namespace is modified; externalTargets avoids injecting a
second rule into the MySQL pod. All live mutations require a reviewed plan.
"""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'deploy/v3/chaos-mesh'
PAUSE = 'experiment.chaos-mesh.org/pause'
CONNECT = 'yeodam-v3-connect'
CONNECTOR = 'yeodam-v3-outbox'
CLUSTER = 'yeodam-v3-foundation'
UUID = re.compile(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}')


class GuardError(ValueError):
    """Only constant, reviewable messages may be shown to the operator."""


def require(test, message):
    if not test:
        raise GuardError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def private_write(path, value):
    path = Path(path)
    require(not path.is_symlink() and not path.exists(), 'Output already exists')
    require(path.parent.is_dir() and not path.parent.is_symlink(), 'Use an existing private output directory')
    require(path.parent.stat().st_mode & 0o077 == 0, 'Output directory must have mode 700')
    with path.open('x', encoding='utf-8') as f:
        os.chmod(path, 0o600)
        json.dump(value, f, indent=2)
        f.write('\n')


def bounded_json(text):
    require(len(text.encode()) <= 4 * 1024 * 1024, 'Response too large')
    return json.loads(text)


class Kube:
    def __init__(self, config):
        self.base = ['kubectl', '--kubeconfig', str(config), '--request-timeout=15s']

    def call(self, args, source=None, timeout=20):
        result = subprocess.run(self.base + args, input=source, text=True,
                                capture_output=True, timeout=timeout,
                                env=dict(os.environ, AWS_PROFILE='yeodam-admin'))
        require(result.returncode == 0, 'Controlled Kubernetes operation failed; output withheld')
        require(len(result.stdout.encode()) <= 4 * 1024 * 1024, 'Response too large')
        return result.stdout

    def get(self, kind, namespace=None, name=None, selector=None):
        args = (['-n', namespace] if namespace else []) + ['get', kind]
        if name:
            args.append(name)
        if selector:
            args += ['-l', selector]
        return bounded_json(self.call(args + ['-o', 'json']))

    def identity(self):
        # Only examine the exec binding in memory. Never save raw kubeconfig.
        c = bounded_json(self.call(['config', 'view', '--minify', '-o', 'json']))
        e = c['users'][0]['user'].get('exec', {})
        args = e.get('args', [])
        require(Path(e.get('command', '')).name == 'aws', 'Expected AWS EKS exec authentication')
        require('get-token' in args and '--cluster-name' in args and
                args[args.index('--cluster-name') + 1] == CLUSTER, 'Wrong EKS cluster binding')
        require('--region' in args and args[args.index('--region') + 1] == 'ap-northeast-2', 'Wrong region')
        require(not any(v.get('name') == 'AWS_PROFILE' and v.get('value') != 'yeodam-admin'
                        for v in e.get('env', [])), 'Unexpected exec profile override')
        if '--profile' in args:
            require(args[args.index('--profile') + 1] == 'yeodam-admin', 'Unexpected AWS profile')
        require('--role-arn' not in args, 'Review role-assuming kubeconfig separately')
        sts = subprocess.run(['aws', '--profile', 'yeodam-admin', '--region', 'ap-northeast-2',
                              'sts', 'get-caller-identity', '--output', 'json'],
                             capture_output=True, text=True, timeout=20)
        require(sts.returncode == 0 and bounded_json(sts.stdout).get('Account') == '483175530259', 'Wrong AWS account or expired session')
        require(not c['clusters'][0]['cluster'].get('insecure-skip-tls-verify'), 'TLS verification required')
        return self.get('namespace', name='kube-system')['metadata']['uid']


def ready_pod(pod):
    return (not pod['metadata'].get('deletionTimestamp') and not pod['spec'].get('hostNetwork') and
            pod['status'].get('phase') == 'Running' and
            any(c.get('type') == 'Ready' and c.get('status') == 'True'
                for c in pod['status'].get('conditions', [])))


def pod_record(p):
    require(ready_pod(p), 'Target must be a Ready non-hostNetwork pod')
    owner = p['metadata'].get('ownerReferences', [])
    require(len(owner) == 1 and owner[0].get('controller') is True, 'Expected one controller owner')
    return {'name': p['metadata']['name'], 'uid': p['metadata']['uid'],
            'node': p['spec']['nodeName'], 'ip': p['status']['podIP'],
            'ownerKind': owner[0]['kind'], 'ownerName': owner[0]['name']}


def snapshot(k):
    uid = k.identity()
    ps = k.get('pods', 'kafka', selector='strimzi.io/cluster=' + CONNECT)['items']
    require(len(ps) == 1, 'Exactly one Connect pod, including terminating pods, required')
    cp = ps[0]
    require(cp['metadata']['labels'].get('strimzi.io/kind') == 'KafkaConnect', 'Wrong Connect workload')
    service = k.get('service', 'mysql', 'mysql-primary')
    require(service['spec'].get('type', 'ClusterIP') == 'ClusterIP' and
            service['spec']['ports'] == [{'name': 'mysql', 'port': 3306, 'protocol': 'TCP', 'targetPort': 'mysql'}],
            'Review changed MySQL Service ports before preparing a fault')
    endpoints = k.get('endpointslices', 'mysql', selector='kubernetes.io/service-name=mysql-primary')['items']
    ready = [e for s in endpoints for e in s.get('endpoints', []) if e.get('conditions', {}).get('ready') is True]
    require(len(ready) == 1 and len(ready[0]['addresses']) == 1, 'Exactly one ready MySQL endpoint required')
    target = ready[0].get('targetRef', {})
    require(target.get('kind') == 'Pod' and target.get('namespace', 'mysql') == 'mysql', 'Wrong MySQL endpoint identity')
    mp = k.get('pod', 'mysql', target['name'])
    mysql = pod_record(mp)
    require(mysql['ownerKind'] == 'StatefulSet' and mysql['ownerName'] == 'mysql' and
            mysql['ip'] == ready[0]['addresses'][0] and target.get('uid') == mysql['uid'], 'MySQL endpoint/owner mismatch')
    nodes = k.get('nodes')['items']
    system = [n['metadata']['name'] for n in nodes if n['metadata']['labels'].get('yeodam.io/role') == 'system'
              and not n.get('spec', {}).get('unschedulable') and any(
                  c['type'] == 'Ready' and c['status'] == 'True' for c in n['status'].get('conditions', []))]
    connect = pod_record(cp)
    require(connect['node'] in system and len(system) >= 2, 'Two existing Ready system nodes required')
    for n in nodes:
        if n['metadata']['name'] == connect['node']:
            info = n['status']['nodeInfo']
            require(info['containerRuntimeVersion'].startswith('containerd://') and
                    info['operatingSystem'] == 'linux' and info['architecture'] == 'amd64', 'Review runtime compatibility')
    argo = k.get('application', 'argocd', 'yeodam-v3-apps')
    require(argo['spec']['source']['targetRevision'] == 'deploy/v3-approved' and
            argo['status']['health']['status'] == 'Healthy' and argo['status']['sync']['status'] == 'Synced',
            'Approved application must be Synced and Healthy')
    return {'version': 1, 'capturedAt': now(), 'cluster': CLUSTER, 'kubeSystemUid': uid,
            'approvedRevision': argo['status']['sync']['revision'], 'connect': connect, 'mysql': mysql,
            'serviceIp': service['spec']['clusterIP'], 'controllerNode': sorted(set(system) - {connect['node']})[0]}


def validate_snapshot(s):
    require(s.get('version') == 1 and s.get('cluster') == CLUSTER, 'Wrong snapshot identity')
    for role in ('connect', 'mysql'):
        p = s[role]
        require(re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', p['name']) and
                UUID.fullmatch(p['uid']) and re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', p['node']), 'Invalid pod identity')
    require(re.fullmatch('[a-f0-9]{40}', s['approvedRevision']) and UUID.fullmatch(s['kubeSystemUid']), 'Invalid revision/cluster UID')
    for ip in (s['connect']['ip'], s['mysql']['ip']):
        a = ipaddress.ip_address(ip)
        require(a.version == 4 and a in ipaddress.ip_network('10.40.0.0/16'), 'Only V3 IPv4 addresses accepted')
    require(ipaddress.ip_address(s['serviceIp']) in ipaddress.ip_network('172.20.0.0/16'), 'Wrong EKS Service IP')
    require(s['mysql']['ownerKind'] == 'StatefulSet' and s['mysql']['ownerName'] == 'mysql', 'Wrong MySQL owner')
    require(s['connect']['name'].startswith(CONNECT + '-') and
            s['controllerNode'] != s['connect']['node'], 'Unexpected Connect/controller placement')
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(s['capturedAt'])).total_seconds()
    require(0 <= age <= 300, 'Snapshot must be refreshed within five minutes')


def prepare(s, run_id, seconds):
    validate_snapshot(s)
    require(re.fullmatch(r'cdc-[a-z0-9-]{1,40}', run_id), 'Use a short cdc- run ID')
    require(type(seconds) is int and 15 <= seconds <= 120, 'First experiment bound is 15..120 seconds')
    # Include both the DNAT peer and virtual Service IP, never a CIDR/DNS wildcard.
    fault = {'apiVersion': 'chaos-mesh.org/v1alpha1', 'kind': 'NetworkChaos',
             'metadata': {'name': run_id, 'namespace': 'kafka', 'annotations': {PAUSE: 'true'},
                          'labels': {'yeodam.io/issue': '656', 'yeodam.io/experiment': run_id}},
             'spec': {'action': 'partition', 'mode': 'all',
                      'selector': {'namespaces': ['kafka'], 'pods': {'kafka': [s['connect']['name']]}},
                      'direction': 'to', 'externalTargets': sorted({s['mysql']['ip'] + ':3306', s['serviceIp'] + ':3306'}),
                      'duration': str(seconds) + 's'}}
    p = {'version': 1, 'createdAt': now(), 'runId': run_id, 'seconds': seconds, 'snapshot': s,
         'scope': 'Connect egress to reviewed MySQL endpoint/Service IPv4:3306 only',
         'fault': fault, 'toolSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    return dict(p, planSha256=sha(p))


def verify_plan(p, expected):
    payload = {k: v for k, v in p.items() if k != 'planSha256'}
    require(p.get('planSha256') == expected == sha(payload), 'Plan hash mismatch')
    rebuilt = prepare(p['snapshot'], p['runId'], p['seconds'])
    require(p['fault'] == rebuilt['fault'] and p['scope'] == rebuilt['scope'] and
            p['toolSha256'] == rebuilt['toolSha256'], 'Changed fault scope or tool version')


def compare_live(p, live):
    validate_snapshot(live)
    for key in ('cluster', 'kubeSystemUid', 'approvedRevision', 'connect', 'mysql', 'serviceIp', 'controllerNode'):
        require(p['snapshot'][key] == live[key], 'Live target or deployment changed; prepare a new plan')


def owned_fault(obj, p, uid):
    require(obj['metadata']['uid'] == uid and obj['metadata']['name'] == p['runId'] and
            obj['metadata']['labels'].get('yeodam.io/experiment') == p['runId'] and
            obj['spec'] == p['fault']['spec'], 'Fault ownership/spec mismatch; refuse broad cleanup')


def recover(k, p, uid):
    obj = k.get('networkchaos', 'kafka', p['runId'])
    owned_fault(obj, p, uid)
    patch = [{'op': 'test', 'path': '/metadata/uid', 'value': uid},
             {'op': 'test', 'path': '/metadata/resourceVersion', 'value': obj['metadata']['resourceVersion']},
             {'op': 'add', 'path': '/metadata/annotations/experiment.chaos-mesh.org~1pause', 'value': 'true'}]
    k.call(['-n', 'kafka', 'patch', 'networkchaos', p['runId'], '--type=json', '-p', json.dumps(patch)])
    # Never remove finalizers/force-delete if recovery is blocked.
    k.call(['-n', 'kafka', 'wait', 'networkchaos/' + p['runId'], '--for=condition=AllRecovered', '--timeout=30s'], timeout=35)
    return {'controllerReportedRecovered': True, 'dataPathRecoveryVerified': False,
            'note': 'Retain paused resource until data-path recovery is independently checked'}


def execute(k, p, expected, receipt_path):
    verify_plan(p, expected)
    require(not Path(str(receipt_path) + '.finished.json').exists(), 'Finished receipt already exists')
    compare_live(p, snapshot(k))
    # A new run cannot coexist even with a paused/unrecovered older experiment.
    require(not k.get('networkchaos', 'kafka')['items'], 'Existing NetworkChaos requires review/cleanup')
    connector = k.get('kafkaconnector', 'kafka', CONNECTOR)
    status = connector.get('status', {})
    require(connector['spec'].get('state') == 'running' and
            status.get('observedGeneration') == connector['metadata']['generation'] and
            status.get('connectorStatus', {}).get('connector', {}).get('state') == 'RUNNING' and
            [(t.get('id'), t.get('state')) for t in status.get('connectorStatus', {}).get('tasks', [])] == [(0, 'RUNNING')],
            'CDC must already be running before injecting a new fault')
    ds = k.get('daemonset', 'chaos-mesh', 'chaos-daemon')
    require(ds['spec']['template']['spec']['nodeSelector'] == {'yeodam.io/role': 'system', 'kubernetes.io/hostname': p['snapshot']['connect']['node']}
            and ds['status'].get('numberReady') == ds['status'].get('desiredNumberScheduled') == 1,
            'Exactly one reviewed Chaos daemon must be Ready')
    created = bounded_json(k.call(['create', '-f', '-', '-o', 'json'], json.dumps(p['fault'])))
    uid = created['metadata']['uid']
    created_at = datetime.fromisoformat(created['metadata']['creationTimestamp'].replace('Z', '+00:00'))
    creation_age = (datetime.now(timezone.utc) - created_at).total_seconds()
    receipt = {'planSha256': expected, 'runId': p['runId'], 'faultUid': uid, 'createdAt': now(),
               'injectionConfirmed': False, 'recovery': None, 'businessRecoveryVerified': False}
    # Persist exact recovery identity BEFORE unpausing. Never overwrite an older run.
    private_write(receipt_path, receipt)
    def interrupted(*_):
        raise KeyboardInterrupt()
    old = signal.signal(signal.SIGTERM, interrupted)
    try:
        require(0 <= creation_age <= 5, 'Paused resource creation consumed the fault window; do not resume')
        obj = k.get('networkchaos', 'kafka', p['runId']); owned_fault(obj, p, uid)
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': uid},
                 {'op': 'test', 'path': '/metadata/resourceVersion', 'value': obj['metadata']['resourceVersion']},
                 {'op': 'replace', 'path': '/metadata/annotations/experiment.chaos-mesh.org~1pause', 'value': 'false'}]
        started = time.monotonic()
        k.call(['-n', 'kafka', 'patch', 'networkchaos', p['runId'], '--type=json', '-p', json.dumps(patch)])
        receipt['activationRequestedAt'] = now()
        receipt['durationStartsAtCreation'] = created['metadata']['creationTimestamp']
        while time.monotonic() - started < p['seconds']:
            obj = k.get('networkchaos', 'kafka', p['runId']); owned_fault(obj, p, uid)
            if any(c.get('type') == 'AllInjected' and c.get('status') == 'True'
                   for c in obj.get('status', {}).get('conditions', [])):
                if not receipt['injectionConfirmed']:
                    receipt.update(injectionConfirmed=True, controllerInjectionObservedAt=now())
            time.sleep(1)
    finally:
        signal.signal(signal.SIGTERM, old)
        try:
            receipt['recovery'] = recover(k, p, uid)
        finally:
            receipt['finishedAt'] = now()
            # New file preserves the initial recovery identity even on interruption.
            private_write(str(receipt_path) + '.finished.json', receipt)
    return receipt


def trip_ids(values):
    require(1 <= len(values) <= 10 and len(set(values)) == len(values), 'Specify 1..10 distinct trial trip IDs')
    require(all(type(v) is int and 0 < v < 2**63 for v in values), 'Invalid trial trip ID')
    return ','.join(str(v) for v in values)


def evidence_sql(ids):
    selected = trip_ids(ids)
    # No payload, filenames, location, users, tokens or arbitrary SQL identifiers.
    projections = {
        'conversion_outbox': ('attachment_conversion_outbox', "'outboxId',outbox_id,'taskId',task_id,'deliveryNo',delivery_no,'tripId',trip_id,'status',status,'createdAt',created_at,'publishedAt',published_at,'partition',record_partition,'offset',record_offset,'brokerTimestamp',broker_timestamp"),
        'creation_outbox': ('trip_creation_outbox', "'outboxId',outbox_id,'executionId',execution_id,'tripId',trip_id,'status',status,'createdAt',created_at,'publishedAt',published_at"),
        'conversion_task': ('attachment_conversion_tasks', "'taskId',task_id,'tripId',trip_id,'status',status"),
        'creation_execution': ('trip_creation_executions', "'executionId',execution_id,'tripId',trip_id,'status',status,'statusVersion',status_version,'displayStatus',display_status,'attachmentCount',total_attachment_count"),
    }
    sql = 'SET SESSION MAX_EXECUTION_TIME=3000; START TRANSACTION READ ONLY;\n'
    for section, (table, fields) in projections.items():
        cdc_filter = ' AND cdc_managed = TRUE' if section.endswith('_outbox') else ''
        sql += f"SELECT JSON_OBJECT('section','{section}',{fields}) FROM yeodam.{table} WHERE trip_id IN ({selected}){cdc_filter} LIMIT 201;\n"
    sql += f"SELECT JSON_OBJECT('section','attachment_counts','tripId',trip_id,'activeRows',COUNT(*),'distinctFileIds',COUNT(DISTINCT file_id)) FROM yeodam.trip_attachments WHERE trip_id IN ({selected}) AND deleted_at IS NULL GROUP BY trip_id LIMIT 11;\n"
    return sql + 'COMMIT;\n'


DB_SHELL = '''set -Eeuo pipefail
umask 077
source /runtime/common.sh
f=$(mktemp /tmp/cdc-evidence-XXXXXX.cnf)
trap 'rm -f "$f"' EXIT
client_file root local "$f" root
mysql --defaults-extra-file="$f" --batch --raw --skip-column-names --silent
'''


def collect_db(k, ids):
    s = snapshot(k)
    raw = k.call(['-n', 'mysql', 'exec', '-i', s['mysql']['name'], '-c', 'mysql', '--', 'bash', '-c', DB_SHELL],
                 evidence_sql(ids))
    rows = [bounded_json(line) for line in raw.splitlines() if line]
    require(all(row.get('tripId') in ids for row in rows), 'Unexpected trial ID in evidence')
    counts = {section: sum(x['section'] == section for x in rows)
              for section in ('conversion_outbox', 'creation_outbox', 'conversion_task', 'creation_execution', 'attachment_counts')}
    return {'version': 1, 'observedAt': now(), 'approvedRevision': s['approvedRevision'],
            'tripIds': ids, 'rows': rows, 'coverage': 'truncated' if max(counts.values()) > 200 else 'bounded-trial-only',
            'kafkaEvidenceMode': 'receipt-listener-observation-not-raw-consumer',
            'rawDeliveryCount': None, 'duplicateBusinessEffects': None, 'verdict': 'not_assessed'}


def compare_evidence(before, after):
    require(before['tripIds'] == after['tripIds'] and before['approvedRevision'] == after['approvedRevision'],
            'Compare the same trial IDs and approved deployment')
    require(before['coverage'] == after['coverage'] == 'bounded-trial-only', 'Truncated evidence cannot establish coverage')
    expected = {r['outboxId'] for r in before['rows'] if 'outboxId' in r}
    observed = {r['outboxId'] for r in after['rows'] if 'outboxId' in r and r.get('status') == 'PUBLISHED'}
    duplicate_files = {str(r['tripId']): r['activeRows'] - r['distinctFileIds']
                       for r in after['rows'] if r['section'] == 'attachment_counts'}
    statuses = {r['executionId']: {'status': r['status'], 'displayStatus': r['displayStatus'],
                                  'statusVersion': r['statusVersion']}
                for r in after['rows'] if r['section'] == 'creation_execution'}
    return {'expectedOutboxCount': len(expected), 'receiptObservedCount': len(expected & observed),
            'unconfirmedOutboxIds': sorted(expected - observed), 'rawDeliveryCount': None,
            'duplicateBusinessEffects': None, 'duplicateActiveFileRowsByTrip': duplicate_files,
            'executionStates': statuses, 'businessRecoveryVerified': False,
            'verdict': 'not_assessed' if expected else 'no_committed_trial_evidence'}
