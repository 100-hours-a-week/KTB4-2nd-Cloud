import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('storage_contract', ROOT / 'scripts/check-v3-storage.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class StorageContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = m.platform.load_documents(m.render())

    def test_storage_rule_creates_no_disk_or_workload(self):
        m.validate(self.docs)
        self.assertEqual([d['kind'] for d in self.docs], ['StorageClass'])
        self.assertEqual(self.docs[0]['metadata']['labels']['yeodam.io/stage'], 'v3')

    def test_rejects_destructive_or_wrong_az_binding(self):
        for key, value in [('reclaimPolicy', 'Delete'), ('volumeBindingMode', 'Immediate'),
                           ('allowVolumeExpansion', False), ('provisioner', 'kubernetes.io/aws-ebs')]:
            docs = copy.deepcopy(self.docs); docs[0][key] = value
            with self.subTest(key=key), self.assertRaises(m.platform.ContractError): m.validate(docs)

    def test_rejects_unencrypted_or_wrong_disk_type(self):
        for key, value in [('encrypted','false'), ('type','gp2')]:
            docs = copy.deepcopy(self.docs); docs[0]['parameters'][key] = value
            with self.subTest(key=key), self.assertRaises(m.platform.ContractError): m.validate(docs)

    def test_rejects_default_override_or_class_deletion(self):
        for annotations in [{}, {'helm.sh/resource-policy':'keep', 'storageclass.kubernetes.io/is-default-class':'true'}]:
            docs = copy.deepcopy(self.docs); docs[0]['metadata']['annotations'] = annotations
            with self.assertRaises(m.platform.ContractError): m.validate(docs)

    def test_rejects_pvc_pv_or_extra_resource(self):
        for kind in ['PersistentVolumeClaim', 'PersistentVolume', 'Deployment']:
            with self.subTest(kind=kind), self.assertRaises(m.platform.ContractError):
                m.validate(self.docs + [{'apiVersion':'v1', 'kind':kind}])

    def test_schema(self):
        m.platform.schema_check(m.render())


if __name__ == '__main__': unittest.main()
