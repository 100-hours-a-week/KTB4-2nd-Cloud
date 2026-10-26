import importlib.util
from pathlib import Path
import unittest

s = importlib.util.spec_from_file_location('analysis428', Path(__file__).parents[1]/'scripts/analyze-nginx-create.py')
m = importlib.util.module_from_spec(s); s.loader.exec_module(m)


class ReconciliationTest(unittest.TestCase):
    def test_commit_without_response_and_duplicate_rows_are_not_success(self):
        events = [{'event': 'started', 'userId': 1, 'tripName': 'a'},
                  {'event': 'started', 'userId': 1, 'tripName': 'b'},
                  {'event': 'final', 'userId': 1, 'tripName': 'a', 'outcome': 'confirmed', 'tripId': 10, 'requestId': 'rid'}]
        rows = [{'userId': 1, 'tripName': 'a', 'tripId': 10},
                {'userId': 1, 'tripName': 'b', 'tripId': 11}, {'userId': 1, 'tripName': 'b', 'tripId': 12}]
        r = m.reconcile(events, rows, [{'request_id': 'rid'}])
        self.assertEqual(r['startedWithoutFinal'], 1)
        self.assertEqual(r['committedWithoutConfirmedResponse'], 2)
        self.assertEqual(r['duplicateRowsForStarted'], 1)
        self.assertEqual(r['confirmedMissingDb'], 0)
        self.assertEqual(r['finalsWithIdMissingOrDuplicateAccess'], 0)


if __name__ == '__main__':
    unittest.main()
