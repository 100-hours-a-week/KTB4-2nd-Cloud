"""Guard the dataset shape and ambiguous-commit handling before staging writes."""

from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
import importlib.util
import tempfile


SOURCE = Path(__file__).parents[3] / 'scripts' / 'staging-migration-seed.py'
SPEC = importlib.util.spec_from_loader('staging_migration_seed', SourceFileLoader('staging_migration_seed', str(SOURCE)))
seed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seed)


class DatasetContractTest(TestCase):
    def setUp(self):
        self.state = {
            'datasetId': '11111111-1111-4111-8111-111111111111',
            'firstUserId': 707, 'firstTripId': 5, 'firstRegionId': 5,
            'tripCount': 1_000_000, 'batchSize': 10_000,
        }

    def test_first_and_last_batch_preserve_fk_mapping_and_trip_name_limit(self):
        first = seed.batch_sql(self.state, 0)
        last = seed.batch_sql(self.state, 99)
        self.assertTrue(first.startswith('START TRANSACTION;\n'))
        self.assertTrue(first.endswith('COMMIT;\n'))
        self.assertEqual(first.count('@staging.invalid'), 100)
        self.assertEqual(first.count("'COMPLETED'"), 10_000)
        self.assertEqual(first.count("'12110'"), 10_000)
        self.assertIn("(5,707,'s000'", first)
        self.assertIn("(1000004,10706,'s099'", last)
        self.assertIn("(1000004,1000004,'12110'", last)
        self.assertNotIn('DROP ', first)

    def test_observed_counts_can_identify_full_commit_or_partial_collision(self):
        with patch.object(seed, 'mysql', return_value='100\t10000\t10000'):
            self.assertEqual(seed.batch_counts(self.state, 0), [100, 10000, 10000])
        with patch.object(seed, 'mysql', return_value='100\t9999\t10000'):
            self.assertNotEqual(seed.batch_counts(self.state, 0), [100, 10000, 10000])

    def test_full_id_ranges_with_other_rows_are_rejected(self):
        with patch.object(seed, 'scalar', side_effect=['99', '10000', '10000']):
            with self.assertRaisesRegex(RuntimeError, 'do not match the dataset contract'):
                seed.batch_integrity(self.state, 0)
        with patch.object(seed, 'scalar', side_effect=['100', '10000', '10000']):
            seed.batch_integrity(self.state, 0)

    def test_checkpoint_write_is_private_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            seed.persist(path, {'completedBatches': [0]})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn('"completedBatches": [', path.read_text())
