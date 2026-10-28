import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('index455', Path(__file__).parents[2] / 'scripts/migration-index455.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ControlReceiptTests(unittest.TestCase):
    def setUp(self):
        self.host = 'i-0123456789abcdef0'
        self.receipt = {'issue': 455, 'case': 'control30', 'hosts': {'source': self.host}, 'report': {
            'runId': '00000000-0000-4000-8000-000000000001', 'rate': 30, 'phase': 'completed',
            'started': 27001, 'targetRows': 27001, 'pendingAtFinalAudit': 0, 'dropped': 0,
            'k6ExitCode': 0, 'missingRegions': 0, 'sourceBoundaryEqual': True,
            'requestHashEqual': True, 'redisEqual': True, 'authAuditBeforeE2E': True,
            'v1E2E': [[30, 30]], 'v2E2E': [[30, 30]]}}

    def test_matching_completed_control(self):
        m.validate_control(self.receipt, 'source', self.host)

    def test_other_host_or_other_case_refused(self):
        for key, value in [('issue', 408), ('case', 'index30'), ('hosts', {'source': 'i-other'})]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                m.validate_control(dict(self.receipt, **{key: value}), 'source', self.host)

    def test_incomplete_or_failed_control_refused(self):
        for key, value in [('targetRows', 27000), ('pendingAtFinalAudit', 1), ('dropped', 1),
                           ('k6ExitCode', 99), ('requestHashEqual', False), ('redisEqual', False),
                           ('v1E2E', [[30, 29]]), ('v2E2E', None), ('phase', 'prepared')]:
            receipt = copy.deepcopy(self.receipt)
            receipt['report'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                m.validate_control(receipt, 'source', self.host)


if __name__ == '__main__':
    unittest.main()
