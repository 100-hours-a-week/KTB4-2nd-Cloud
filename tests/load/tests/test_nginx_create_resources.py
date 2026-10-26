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

    def test_host_modes_and_container_counters_have_distinct_denominators(self):
        self.assertEqual(resources.cpu_modes([0]*8, [20, 0, 10, 50, 5, 0, 5, 10]),
                         {'user':20, 'nice':0, 'system':10, 'idle':50, 'iowait':5, 'irq':0, 'softirq':5, 'steal':10})
        self.assertIsNone(resources.cpu_modes([2]*8, [1]*8))
        self.assertIsNone(resources.cpu_modes(None, [1]*8))
        self.assertEqual(resources.container_usage(100, 2000100, 1), 200)
        self.assertIsNone(resources.container_usage(100, 99, 1))


if __name__ == '__main__':
    unittest.main()
