import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('retirement', ROOT / 'scripts/check-v2-staging-retirement-plan.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def row(address, kind='aws_instance', before=None, after=None, actions=None):
    return dict(address=address, type=kind, mode='managed', change=dict(
        before=before or {}, after=after, actions=actions or ['no-op']))


class RetirementPlanTests(unittest.TestCase):
    def setUp(self):
        self.plan = {'complete': True, 'resource_changes': [row(a) for a in m.PROTECTED_ADDRESSES]}

    def test_accepts_scoped_service_deletion(self):
        self.plan['resource_changes'].append(row('module.app_storage.aws_s3_bucket.app_data',
            'aws_s3_bucket', {'id': m.SERVICE_BUCKET}, actions=['delete']))
        self.assertEqual(m.validate(self.plan), {'delete': 1})

    def test_rejects_changes_to_any_shared_gpu_identity(self):
        for address in m.PROTECTED_ADDRESSES:
            plan = copy.deepcopy(self.plan)
            next(x for x in plan['resource_changes'] if x['address'] == address)['change']['actions'] = ['update']
            with self.subTest(address=address), self.assertRaises(ValueError):
                m.validate(plan)

    def test_rejects_archive_metadata_deletion_and_unknown_bucket(self):
        for bucket in [*m.ARCHIVES, 'unexpected-bucket']:
            self.plan['resource_changes'].append(row('unexpected', 'aws_s3_bucket', {'id': bucket, 'bucket': bucket}, actions=['delete']))
            with self.subTest(bucket=bucket), self.assertRaises(ValueError):
                m.validate(self.plan)
            self.plan['resource_changes'].pop()

    def test_rejects_production_dependency_deletion_and_missing_protections(self):
        self.plan['resource_changes'].append(row('unexpected', before={'name': 'yeodam-v2-production-ai-api'}, actions=['delete']))
        with self.assertRaises(ValueError): m.validate(self.plan)
        with self.assertRaises(ValueError): m.validate({'complete': True, 'resource_changes': []})

    def test_rejects_enabled_archive_expiration_and_replacements(self):
        self.plan['resource_changes'].append(row('aws_s3_bucket_lifecycle_configuration.mysql_backup',
            'aws_s3_bucket_lifecycle_configuration', {'bucket': 'archive'},
            {'bucket': 'archive', 'rule': [{'id': 'expire', 'status': 'Enabled', 'expiration': [{'days': 14}]}]}, ['update']))
        with self.assertRaises(ValueError): m.validate(self.plan)
        self.plan['resource_changes'][-1]['change']['after']['rule'][0]['status'] = 'Disabled'
        self.assertEqual(m.validate(self.plan), {'update': 1})
        self.plan['resource_changes'][-1]['change']['actions'] = ['delete', 'create']
        with self.assertRaises(ValueError): m.validate(self.plan)

    def test_staging_deployment_workflows_are_absent(self):
        for name in ['release', 'frontend-deploy', 'backend-deploy', 'ai-deploy', 'runpod-update']:
            self.assertFalse((ROOT / f'.github/workflows/v2-staging-{name}.yml').exists())


if __name__ == '__main__': unittest.main()
