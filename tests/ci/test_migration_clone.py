"""Safety contracts for capturing the exact two approved hosts, without AWS calls."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('clone_images', Path(__file__).parents[2] / 'scripts/capture-migration-clone-images.py')
clone = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clone)


class CaptureGuardTests(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.volumes = []
        for role, expected in clone.ORIGINALS.items():
            mapping = []
            for device, size in [('/dev/sda1', 30), ('/dev/sdf', expected['data_gib'])]:
                volume_id = role + device
                mapping.append({'DeviceName': device, 'Ebs': {'VolumeId': volume_id}})
                self.volumes.append({'VolumeId': volume_id, 'Encrypted': True, 'Size': size,
                                     'VolumeType': 'gp3', 'Iops': 3000, 'Throughput': 125})
            self.rows.append({'InstanceId': expected['id'], 'InstanceType': expected['type'],
                              'State': {'Name': 'running'}, 'BlockDeviceMappings': mapping})

    def test_exact_original_contract(self):
        clone.validate_originals(self.rows, self.volumes)

    def test_other_instance_refused(self):
        rows = copy.deepcopy(self.rows)
        rows[0]['InstanceId'] = 'i-unrelated'
        with self.assertRaises(ValueError):
            clone.validate_originals(rows, self.volumes)

    def test_stopped_or_resized_original_refused(self):
        for key, value in [('State', {'Name': 'stopped'}), ('InstanceType', 't3.large')]:
            with self.subTest(key=key):
                rows = copy.deepcopy(self.rows)
                rows[0][key] = value
                with self.assertRaises(ValueError):
                    clone.validate_originals(rows, self.volumes)

    def test_extra_disk_refused(self):
        rows = copy.deepcopy(self.rows)
        rows[0]['BlockDeviceMappings'].append({'DeviceName': '/dev/sdg', 'Ebs': {'VolumeId': 'extra'}})
        with self.assertRaises(ValueError):
            clone.validate_originals(rows, self.volumes)

    def test_storage_or_encryption_drift_refused(self):
        for key, value in [('Encrypted', False), ('Size', 31), ('Iops', 6000), ('Throughput', 250)]:
            with self.subTest(key=key):
                volumes = copy.deepcopy(self.volumes)
                volumes[0][key] = value
                with self.assertRaises(ValueError):
                    clone.validate_originals(self.rows, volumes)

prepare_spec = importlib.util.spec_from_file_location('prepare_clone', Path(__file__).parents[2] / 'scripts/prepare-migration-clone.py')
prepare = importlib.util.module_from_spec(prepare_spec)
prepare_spec.loader.exec_module(prepare)


class RestoreGuardTests(unittest.TestCase):
    def setUp(self):
        self.instance = 'i-0123456789abcdef0'
        self.volume = 'vol-0123456789abcdef0'
        self.document = {'instanceId': self.instance, 'accountId': '483175530259', 'region': 'ap-northeast-2'}
        self.marker = {'issue': 444, 'role': 'source'}

    def test_matching_clone_and_dedicated_disk(self):
        prepare.guard('source', self.instance, self.volume, self.document, self.marker, self.volume.replace('-', ''))

    def test_original_instance_never_restored(self):
        for original in prepare.ORIGINALS:
            with self.subTest(original=original), self.assertRaises(ValueError):
                document = dict(self.document, instanceId=original)
                prepare.guard('source', original, self.volume, document, self.marker, self.volume)

    def test_wrong_account_region_role_or_volume(self):
        for key, value in [('accountId', '000000000000'), ('region', 'us-east-1'), ('instanceId', 'i-another')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                prepare.guard('source', self.instance, self.volume, dict(self.document, **{key: value}), self.marker, self.volume)
        with self.assertRaises(ValueError):
            prepare.guard('target', self.instance, self.volume, self.document, self.marker, self.volume)
        with self.assertRaises(ValueError):
            prepare.guard('source', self.instance, self.volume, self.document, self.marker, 'vol-another')


class SourceAddressTests(unittest.TestCase):
    def test_new_source_only(self):
        self.assertEqual(prepare.validate_source_address('10.30.0.245'), '10.30.0.245')
        for value in ['10.30.0.174', '10.30.10.45', '127.0.0.1', "10.30.0.1'; DROP USER x", '::1']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                prepare.validate_source_address(value)


if __name__ == '__main__':
    unittest.main()
