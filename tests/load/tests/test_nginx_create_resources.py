import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('resources428', Path(__file__).parents[1]/'scripts/nginx-create-resources.py')
resources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resources)


class ResourceTest(unittest.TestCase):
    def test_cpu_normalizes_by_actual_interval_and_rejects_reused_pid(self):
        self.assertEqual(resources.process_percent((100, 42), (160, 42), 2, 100), 30)
        self.assertIsNone(resources.process_percent((100, 42), (160, 43), 2, 100))
        self.assertIsNone(resources.process_percent(None, (160, 42), 2, 100))
        self.assertIsNone(resources.process_percent((160, 42), (100, 42), 2, 100))

    def test_proc_stat_handles_spaces_and_parentheses_in_comm(self):
        fields = ['S'] + ['0'] * 49
        fields[11], fields[12], fields[19] = '100', '20', '999'
        self.assertEqual(resources.parse_stat('123 (a worker (thread)) '+' '.join(fields)), (120, 999))


if __name__ == '__main__':
    unittest.main()
