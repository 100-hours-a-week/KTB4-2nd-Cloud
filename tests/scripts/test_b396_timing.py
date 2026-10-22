import importlib.util
from pathlib import Path
import unittest
import json
import tempfile
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('analyze396', ROOT / 'scripts/b-integration396-analyze.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def row(partition=0, start=1000, apply=1100, end=1200):
    return {'origin': 'source', 'partition': partition, 'queuedAtMs': start,
            'consumerStartedAtMs': apply, 'committedAtMs': end, 'applyElapsedUs': (end - apply) * 1000}


class TimingEvidenceTests(unittest.TestCase):
    def test_incomplete_timing_file_never_claims_all_request_backlog(self):
        r = m.analyze({'a': row()}, 1, 2, 1)
        self.assertFalse(r['allExpectedCompleted'])
        self.assertIn('excludes unapplied', r['pendingScope'])

    def test_window_occupancy_clips_crossing_work_and_reports_partition_tail(self):
        r = m.analyze({'a': row(7, 1000, 1500, 2500),
                       'b': row(0, 1000, 1000, 1100)}, 1, 2, 1)
        self.assertAlmostEqual(r['bins'][0]['source']['meanActiveObserved'], .6)
        self.assertAlmostEqual(r['bins'][1]['source']['meanActiveObserved'], .5)
        self.assertEqual(r['bins'][0]['observedPendingAtEnd'], 1)
        self.assertTrue(r['allExpectedCompleted'])
        self.assertEqual(r['bins'][1]['source']['completedByPartition'], {7: 1})

    def test_clock_errors_are_not_silently_converted_to_negative_queue(self):
        with self.assertRaises(ValueError):
            m.analyze({'a': row(start=1100, apply=1000)}, 1, 1)

    def test_output_excludes_unrecognized_sensitive_fields(self):
        entry = row();entry['sql'] = 'SENSITIVE_MARKER';entry['accessToken'] = 'SENSITIVE_MARKER'
        self.assertNotIn('SENSITIVE_MARKER', str(m.analyze({'a': entry}, 1, 1)))

    def test_completion_and_batch_collect_only_allowed_metadata(self):
        spec = importlib.util.spec_from_file_location('app396', ROOT / 'scripts/b-integration376-app.py')
        app = importlib.util.module_from_spec(spec);spec.loader.exec_module(app)
        run='51ad4c26-127f-48a4-82d7-524ddce311d1'
        rid='4199673b-0fe2-4fe7-a68c-9bf8b31105c7'
        line=(f'migration_trip_completed request_id={rid} origin=source queued_at_ms=1000 committed_at_ms=1100 '
              'consumer_started_at_ms=1050 apply_elapsed_us=50000 partition=7 diagnostic=received_ms=1001 scheduler_wait_us=49000 connection_count=1 sql_secret=42\n'
              'migration_trip_batch origin=source at_ms=1001 thread=listener-1 assigned=[topic-6, topic-7] counts={7=1} first={7=20} last={7=20} active=4 queued=8\n')
        payload=line.encode();framed=b'\x01\x00\x00\x00'+len(payload).to_bytes(4,'big')+payload
        connection=MagicMock();response=connection.getresponse.return_value;response.status=200;response.read.return_value=framed
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/run).mkdir()
            with patch.object(app,'Path',return_value=root),patch.object(app,'Connection',return_value=connection):
                app.collect_completions('owned',run)
            row=json.loads((root/run/'completions.json').read_text())[rid]
            self.assertEqual(row['diagnostic']['connection_count'],1)
            self.assertNotIn('sql_secret',row['diagnostic'])
            batch=json.loads((root/run/'diagnostic-batches.json').read_text())[0]
            self.assertEqual(batch['assignedPartitions'],[6,7])
            self.assertEqual(batch['counts'],{'7':1})


if __name__ == '__main__':
    unittest.main()
