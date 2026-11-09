"""Regression for Healthy apps with missing Ingress or failed CDC Task."""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
s = importlib.util.spec_from_file_location('readiness', ROOT / 'scripts/check-v3-qa-readiness.py')
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)


def evidence():
    app = {'metadata': {'name': 'yeodam-v3-apps', 'namespace': 'argocd'},
           'spec': {'source': {'targetRevision': 'deploy/v3-approved'}},
           'status': {'sync': {'status': 'Synced', 'revision': 'a' * 40}, 'health': {'status': 'Healthy'}}}
    deploys = {'items': [{'metadata': {'name': name, 'namespace': ns, 'generation': 1}, 'spec': {'replicas': 1},
                         'status': {'observedGeneration': 1, 'readyReplicas': 1, 'updatedReplicas': 1, 'availableReplicas': 1}}
                        for name, ns in m.APP_NAMES.items()]}
    ingresses = {'items': []}
    for route in m.ROUTES['routes']:
        service = {'name': 'reject', 'port': {'name': 'use-annotation'}} if 'action' in route else {'name': route['service'], 'port': {'number': route['port']}}
        ingresses['items'].append({'metadata': {'namespace': route['namespace'], 'name': 'v3-' + route['name'],
            'annotations': {'alb.ingress.kubernetes.io/group.name': m.ROUTES['group'], 'alb.ingress.kubernetes.io/group.order': str(route['order'])}},
            'spec': {'ingressClassName': 'yeodam-v3-alb', 'rules': [{'host': m.ROUTES['host'], 'http': {'paths': [
                {'path': path, 'pathType': kind, 'backend': {'service': service}} for path, kind in route['paths']]}}]}})
    kafka = {'items': []}
    for kind, name in [('Kafka', 'yeodam-v3-kafka'), ('KafkaConnect', 'yeodam-v3-connect'), ('KafkaConnector', 'yeodam-v3-outbox')]:
        kafka['items'].append({'kind': kind, 'metadata': {'namespace': 'kafka', 'name': name, 'generation': 1},
            'spec': {'state': 'running', 'config': {'snapshot.mode': 'initial'}},
            'status': {'observedGeneration': 1, 'conditions': [{'type': 'Ready', 'status': 'True'}],
                'connectorStatus': {'connector': {'state': 'RUNNING'}, 'tasks': [{'id': 0, 'state': 'RUNNING'}]}}})
    targets = [{'service': s, 'states': ['healthy']} for s in sorted(m.TARGETS)]
    return [app, deploys, ingresses, kafka, targets, 'a' * 40]


class Readiness(unittest.TestCase):
    def test_complete_infrastructure_is_not_functional_qa_proof(self):
        result = m.validate(*evidence())
        self.assertTrue(result['infrastructureReadyForQa'])
        self.assertFalse(result['functionalQaCompleted'])
        self.assertFalse(result['resourcesChanged'])

    def test_missing_trip_or_photo_route_rejected_even_with_healthy_apps(self):
        for name in ['v3-trip', 'v3-photo']:
            e = evidence();e[2]['items'] = [i for i in e[2]['items'] if i['metadata']['name'] != name]
            with self.assertRaisesRegex(ValueError, 'external route'):m.validate(*e)

    def test_failed_cdc_task_rejected_even_with_ready_connector_and_apps(self):
        for change in ['failed', 'missing', 'stale', 'notready', 'recovery-mode']:
            e = evidence();c = e[3]['items'][-1]
            if change == 'failed':c['status']['connectorStatus']['tasks'][0]['state'] = 'FAILED'
            if change == 'missing':c['status']['connectorStatus']['tasks'] = []
            if change == 'stale':c['status']['observedGeneration'] = 0
            if change == 'notready':c['status']['conditions'][0]['status'] = 'False'
            if change == 'recovery-mode':c['spec']['config']['snapshot.mode'] = 'when_needed'
            with self.subTest(change=change), self.assertRaises(ValueError):m.validate(*e)

    def test_stale_app_disabled_replica_and_unhealthy_target_rejected(self):
        for change in ['stale', 'disabled', 'missing', 'target', 'host', 'order', 'commit']:
            e = evidence()
            if change == 'commit':e[0]['status']['sync']['revision'] = 'b' * 40
            if change == 'stale':e[1]['items'][0]['status']['observedGeneration'] = 0
            if change == 'disabled':e[1]['items'][0]['spec']['replicas'] = 0
            if change == 'missing':e[1]['items'].pop()
            if change == 'target':e[4][0]['states'] = ['unhealthy']
            if change == 'host':e[2]['items'][0]['spec']['rules'][0]['host'] = 'yeodam-2gether.com'
            if change == 'order':e[2]['items'][0]['metadata']['annotations']['alb.ingress.kubernetes.io/group.order'] = '999'
            with self.subTest(change=change), self.assertRaises(ValueError):m.validate(*e)


if __name__ == '__main__':unittest.main()
