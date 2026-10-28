import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('h2442', Path(__file__).parents[1]/'scripts/analyze-nginx-h2-replay.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class AuditTest(unittest.TestCase):
    def test_status_zero_can_hide_a_commit_and_must_be_audited(self):
        events = [{'event': 'started', 'userId': 1, 'tripName': 'own'},
                  {'event': 'response', 'userId': 1, 'tripName': 'own', 'status': 0, 'requestId': None}]
        db = [{'userId': 1, 'tripName': 'own', 'tripId': 3}]
        r = audit.reconcile(events, db, [])
        self.assertEqual(r['dbRowsWithout201'], 1)
        self.assertEqual(r['confirmed201'], 0)

    def test_201_requires_matching_id_and_unique_server_log(self):
        events = [{'event': 'started', 'userId': 1, 'tripName': 'own'},
                  {'event': 'response', 'userId': 1, 'tripName': 'own', 'status': 201, 'tripId': 3, 'requestId': 'rid'}]
        r = audit.reconcile(events, [], [{'request_id': 'rid'}, {'request_id': 'rid'}])
        self.assertEqual(r['confirmedMissingDb'], 1)
        self.assertEqual(r['responseIdsMissingOrDuplicateNginx'], 1)

    def test_interrupted_request_stays_visible(self):
        r = audit.reconcile([{'event': 'started', 'userId': 1, 'tripName': 'own'}], [], [])
        self.assertEqual(r['startedWithoutResponse'], 1)


if __name__ == '__main__': unittest.main()
