import copy
import importlib.util
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('karpenter', ROOT / 'scripts/check-v3-karpenter.py')
k = importlib.util.module_from_spec(spec)
spec.loader.exec_module(k)


class KarpenterContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = Path(os.environ['V3_HELM']).parent
        operator, access = k.render(directory)
        cls.operator = k.platform.load_documents(operator)
        cls.access = k.platform.load_documents(access)

    def test_baseline(self):
        k.validate_operator(self.operator)
        k.validate_access(self.access)

    def test_capacity_objects_rejected(self):
        for kind in ('NodePool', 'EC2NodeClass', 'NodeClaim', 'CapacityBuffer'):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                k.validate_operator(self.operator + [{'kind': kind, 'metadata': {'name': 'unexpected'}}])

    def test_provisioned_nodes_single_replica_unpinned_image_and_extra_identity_rejected(self):
        for change in ('node', 'replicas', 'image', 'env', 'gate', 'probe', 'rbac'):
            docs = copy.deepcopy(self.operator)
            d = next(d for d in docs if d['kind'] == 'Deployment')
            pod = d['spec']['template']['spec']; c = pod['containers'][0]
            if change == 'node': pod['nodeSelector']['yeodam.io/role'] = 'api'
            if change == 'replicas': d['spec']['replicas'] = 1
            if change == 'image': c['image'] = 'public.ecr.aws/karpenter/controller:latest'
            if change == 'env': c['env'].append({'name': 'AWS_REGION', 'value': 'us-east-1'})
            if change == 'gate': next(e for e in c['env'] if e['name'] == 'FEATURE_GATES')['value'] = 'NodeRepair=true'
            if change == 'probe': c['readinessProbe']['httpGet']['path'] = '/'
            if change == 'rbac': next(d for d in docs if d['kind'] == 'ClusterRole')['rules'].append({'apiGroups': [''], 'resources': ['secrets'], 'verbs': ['get']})
            with self.subTest(change=change), self.assertRaises(ValueError): k.validate_operator(docs)

    def test_broad_network_and_unrestricted_namespace_rejected(self):
        for change in ('egress', 'ingress', 'psa', 'default'):
            docs = copy.deepcopy(self.access)
            policies = {d['metadata']['name']: d for d in docs if d['kind'] == 'NetworkPolicy'}
            if change == 'egress': policies['controller-egress']['spec']['egress'].append({})
            if change == 'ingress': policies['controller-health']['spec']['ingress'][0]['from'] = [{'ipBlock': {'cidr': '0.0.0.0/0'}}]
            if change == 'psa': next(d for d in docs if d['kind'] == 'Namespace')['metadata']['labels']['pod-security.kubernetes.io/enforce'] = 'privileged'
            if change == 'default': policies['default-deny']['spec']['ingress'] = [{}]
            with self.subTest(change=change), self.assertRaises(ValueError): k.validate_access(docs)

    def test_aggregation_filter_is_fail_closed(self):
        with self.assertRaises(ValueError): k.post.filter_manifest('kind: NodePool\nmetadata: {name: surprise}')
        self.assertFalse(any('rbac.authorization.k8s.io/aggregate-to-admin' in d['metadata'].get('labels', {}) for d in self.operator))
