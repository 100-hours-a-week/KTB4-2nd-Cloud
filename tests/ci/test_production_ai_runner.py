import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('production_runner', ROOT / 'scripts/install-v2-production-ai-runner.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ProductionRunnerBoundaryTests(unittest.TestCase):
    def test_wrong_host_rejected_before_commands(self):
        responses = [Mock(read=lambda: b'token'), Mock(read=lambda: b'i-wrong')]
        with patch.object(m.os, 'geteuid', return_value=0), \
             patch.object(m.urllib.request, 'urlopen', side_effect=responses), \
             patch.object(m, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'Wrong shared GPU host'):
                m.host_guard()
            run.assert_not_called()

    def test_wrong_data_volume_rejected(self):
        responses = [Mock(read=lambda: b'token'), Mock(read=lambda: b'i-05e1ad9dd96dba4ae')]
        with patch.object(m.os, 'geteuid', return_value=0), \
             patch.object(m.urllib.request, 'urlopen', side_effect=responses), \
             patch.object(m, 'run', side_effect=['', '/dev/fixture', 'vol-wrong']):
            with self.assertRaisesRegex(RuntimeError, 'Wrong Qdrant data EBS'):
                m.host_guard()

    def test_existing_api_key_is_preserved(self):
        value = 'a' * 64
        response = SimpleNamespace(returncode=0, stdout=json.dumps({'Parameter': {'Value': value}}))
        with patch.object(m.subprocess, 'run', return_value=response), patch.object(m, 'aws') as aws:
            self.assertEqual(m.api_key(), value)
            aws.assert_not_called()


if __name__ == '__main__':
    unittest.main()
