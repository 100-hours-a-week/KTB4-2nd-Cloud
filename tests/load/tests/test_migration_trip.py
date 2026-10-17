import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


script = Path(__file__).parents[1] / 'scripts' / 'reconcile-migration-trip.py'
spec = importlib.util.spec_from_file_location('migration_trip_reconcile', script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

RUN = '6b942bca-39b9-40c7-8af4-8c098e2c3262'
TRIP = 't2e3617d34'
ROW = ('1000005\t707\tt2e3617d34\t2025-01-01\t2025-01-03\tPROCESSING\t'
       '1000005\t50110\tECA09CECA3BC\t33.49953424\t126.53117109\n')


class MigrationTripReconcileTest(unittest.TestCase):
    def run_case(self, status=201, outcome='confirmed', source=ROW, target=ROW, trip_id=1000005):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            events = folder / 'events.jsonl'
            source_path = folder / 'source.tsv'
            target_path = folder / 'target.tsv'
            request = {'tripName': TRIP, 'startDate': '2025-01-01',
                       'endDate': '2025-01-03', 'regionCodes': ['50110']}
            lines = [
                {'source': 'migration-trip-create', 'event': 'started', 'runId': RUN,
                 'userId': 707, 'tripName': TRIP, 'request': request},
                {'source': 'migration-trip-create', 'event': 'final', 'runId': RUN,
                 'userId': 707, 'tripName': TRIP, 'status': status,
                 'tripId': trip_id, 'outcome': outcome},
            ]
            events.write_text(''.join(json.dumps(line) + '\n' for line in lines))
            source_path.write_text(source)
            target_path.write_text(target)
            return module.reconcile(events, source_path, target_path, RUN)

    def test_confirmed_request_must_match_both_tables_on_both_databases(self):
        report = self.run_case()
        self.assertTrue(report['passed'])
        self.assertEqual(report['requests'][0]['classification'], 'confirmed_and_replicated')

    def test_timeout_with_committed_rows_is_not_http_success(self):
        report = self.run_case(status=0, outcome='unconfirmed', trip_id=None)
        self.assertFalse(report['passed'])
        self.assertEqual(report['requests'][0]['classification'], 'committed_without_confirmed_http')

    def test_missing_target_and_duplicate_region_fail(self):
        self.assertFalse(self.run_case(target='')['passed'])
        duplicate = ROW + ROW.replace('\t1000005\t50110\t', '\t1000006\t50110\t')
        report = self.run_case(source=duplicate, target=duplicate)
        self.assertFalse(report['passed'])
        self.assertEqual(report['requests'][0]['source'], 'region_mismatch')


if __name__ == '__main__':
    unittest.main()
