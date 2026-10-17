import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'summarize-migration-trip-diagnostics.py'
SPEC = importlib.util.spec_from_file_location('migration_trip_diagnostic_summary', SCRIPT)
summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(summary)


class DiagnosticSummaryTest(unittest.TestCase):
    def test_window_and_counter_deltas(self):
        with tempfile.TemporaryDirectory() as root:
            cpu, db = (Path(root) / name for name in ('cpu.jsonl', 'db.jsonl'))
            cpu.write_text(''.join(json.dumps(row) + '\n' for row in (
                {'event': 'cpu', 'time': '2026-01-01T00:00:00+00:00', 'hostCpuPct': 99, 'groups': {}},
                {'event': 'cpu', 'time': '2026-01-01T00:00:01+00:00', 'hostCpuPct': 40,
                 'groups': {'java': {'cpuPctOfOneCore': 80}}},
                {'event': 'cpu', 'time': '2026-01-01T00:00:02+00:00', 'hostCpuPct': 60,
                 'groups': {'java': {'cpuPctOfOneCore': 120}}},
            )))
            db.write_text(''.join(json.dumps(row) + '\n' for row in (
                {'event': 'db', 'time': '2026-01-01T00:00:01+00:00',
                 'sourceStatus': 'Questions\t10\nThreads_running\t2\nInnodb_row_lock_waits\t0',
                 'proxyPool': '3\t7\t0\t10', 'proxyDigest': 'abc\t2\t100\t80'},
                {'event': 'db', 'time': '2026-01-01T00:00:02+00:00',
                 'sourceStatus': 'Questions\t20\nThreads_running\t5\nInnodb_row_lock_waits\t1',
                 'proxyPool': '8\t2\t0\t20', 'proxyDigest': 'abc\t5\t400\t150'},
            )))
            start = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
            end = datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc)
            result = summary.summarize(cpu, db, start, end)
            self.assertEqual(result['cpuSamples'], 2)
            self.assertEqual(result['hostCpuPct']['average'], 50)
            self.assertEqual(result['processCpuPctOfOneCore']['java']['maximum'], 120)
            self.assertEqual(result['sourceStatusDelta']['Questions'], 10)
            self.assertEqual(result['sourceStatusDelta']['Innodb_row_lock_waits'], 1)
            self.assertEqual(result['threadsRunningMax'], 5)
            self.assertEqual(result['proxyConnUsedMax'], 8)
            self.assertEqual(result['topDigestDeltas'][0]['sumTimeDeltaUs'], 300)


if __name__ == '__main__':
    unittest.main()
