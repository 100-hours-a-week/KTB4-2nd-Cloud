import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'migration-trip-diagnostics.py'
SPEC = importlib.util.spec_from_file_location('migration_trip_diagnostics', SCRIPT)
diagnostics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostics)


class DiagnosticsTest(unittest.TestCase):
    def test_host_and_process_cpu_units_and_pid_reuse(self):
        self.assertEqual(diagnostics.host_cpu([100, 0, 0, 100, 0, 0, 0, 0],
                                              [130, 0, 0, 170, 0, 0, 0, 0]), 30.0)
        previous = {7: ('java', 100, 3, 1000), 8: ('nginx', 50, 1, 100)}
        current = {7: ('java', 140, 4, 1100), 8: ('k6', 10, 2, 200)}
        groups = diagnostics.cpu_sample(previous, current, 2, 100)
        self.assertEqual(groups['java']['cpuPctOfOneCore'], 20.0)
        self.assertEqual(groups['java']['threads'], 4)
        self.assertEqual(groups['k6']['cpuPctOfOneCore'], 0)


if __name__ == '__main__':
    unittest.main()
