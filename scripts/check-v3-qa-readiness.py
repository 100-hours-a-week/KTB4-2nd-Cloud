#!/usr/bin/env python3
"""Validate read-only runtime evidence before QA; this is not functional E2E."""
import argparse
import json
import re
from pathlib import Path
from v3_delivery_contract import APP_NAMES
from v3_outbox_contract import CONTRACT, require

ROOT = Path(__file__).resolve().parents[1]
ROUTES = json.loads((ROOT / 'deploy/v3/external-access/files/routes.json').read_text())
TARGETS = {'frontend', 'auth-api', 'trip-api', 'photo-api', 'trip-websocket'}


def ready_condition(resource):
    status, meta = resource.get('status', {}), resource['metadata']
    return type(meta.get('generation')) is int and meta['generation'] > 0 and status.get('observedGeneration') == meta['generation'] and any(
        c.get('type') == 'Ready' and c.get('status') == 'True' for c in status.get('conditions', []))


def validate(application, deployments, ingresses, kafka, targets, approved_commit):
    require(application['metadata'].get('namespace') == 'argocd'
            and application['metadata'].get('name') == 'yeodam-v3-apps'
            and application['spec']['source'].get('targetRevision') == 'deploy/v3-approved', 'Wrong approved Application')
    status = application.get('status', {})
    require(re.fullmatch('[a-f0-9]{40}', approved_commit) and status.get('sync', {}).get('revision') == approved_commit, 'Exact current approved Commit must be observed')
    require(status.get('sync', {}).get('status') == 'Synced' and status.get('health', {}).get('status') == 'Healthy',
            'Approved apps must be Synced/Healthy')
    expected = {(ns, name) for name, ns in APP_NAMES.items()}
    actual = {(x['metadata']['namespace'], x['metadata']['name']): x for x in deployments['items']}
    for key in expected:
        x = actual.get(key, {})
        desired = x.get('spec', {}).get('replicas', 0)
        s = x.get('status', {})
        require(type(desired) is int and desired > 0 and s.get('observedGeneration') == x.get('metadata', {}).get('generation')
                and s.get('updatedReplicas', 0) == desired and s.get('readyReplicas', 0) == desired
                and s.get('availableReplicas', 0) == desired, 'App not current/ready: ' + key[1])
    routes = {(x['metadata']['namespace'], x['metadata']['name']): x for x in ingresses['items']}
    for route in ROUTES['routes']:
        x = routes.get((route['namespace'], 'v3-' + route['name']), {})
        expected_service = {'name': 'reject', 'port': {'name': 'use-annotation'}} if 'action' in route else {
            'name': route['service'], 'port': {'number': route['port']}}
        expected_spec = {'ingressClassName': 'yeodam-v3-alb', 'rules': [{'host': ROUTES['host'], 'http': {'paths': [
            {'path': path, 'pathType': kind, 'backend': {'service': expected_service}} for path, kind in route['paths']]}}]}
        annotations = x.get('metadata', {}).get('annotations', {})
        require(x.get('spec') == expected_spec and annotations.get('alb.ingress.kubernetes.io/group.name') == ROUTES['group']
                and annotations.get('alb.ingress.kubernetes.io/group.order') == str(route['order']),
                'Missing/wrong external route: ' + route['name'])
    require({t['service'] for t in targets} == TARGETS and len(targets) == len(TARGETS)
            and all(t.get('states') and all(s == 'healthy' for s in t['states']) for t in targets), 'All five ALB services must have healthy targets')
    kinds = {(x['kind'], x['metadata']['name']): x for x in kafka['items'] if x['metadata'].get('namespace') == 'kafka'}
    for kind, name in [('Kafka', 'yeodam-v3-kafka'), ('KafkaConnect', 'yeodam-v3-connect'), ('KafkaConnector', CONTRACT['connectorName'])]:
        x = kinds.get((kind, name))
        require(x is not None and ready_condition(x), kind + ' is not freshly Ready')
    source = kinds[('KafkaConnector', CONTRACT['connectorName'])]
    s = source['status'].get('connectorStatus', {})
    tasks = s.get('tasks', [])
    require(source['spec'].get('state') == 'running' and source['spec']['config'].get('snapshot.mode') == 'initial'
            and s.get('connector', {}).get('state') == 'RUNNING' and len(tasks) == 1
            and tasks[0].get('id') == 0 and tasks[0].get('state') == 'RUNNING', 'Actual CDC Task 0 must be RUNNING in normal mode')
    return {'infrastructureReadyForQa': True, 'appDeployments': 9, 'albServices': 5,
            'cdcTask0': 'RUNNING', 'functionalQaCompleted': False, 'resourcesChanged': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--approved-commit', required=True, help='Current deploy/v3-approved Head, read from Git')
    p.add_argument('--directory', required=True, type=Path, help='Fresh kubectl/AWS read-only JSON evidence')
    a = p.parse_args()
    names = ['application.json', 'deployments.json', 'ingress.json', 'kafka.json', 'target-health.json']
    result = validate(*[json.loads((a.directory / n).read_text()) for n in names], a.approved_commit)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
