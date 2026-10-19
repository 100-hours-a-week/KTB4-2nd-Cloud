import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('production_parameters', Path(__file__).parents[2] / 'scripts/seed-v2-production-ai-parameters.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ProductionParameterBoundary(unittest.TestCase):
    def test_wrong_account_does_not_read_or_write_parameters(self):
        with patch.object(module, 'aws', return_value={'Account': '000000000000'}), patch.object(module, 'parameter') as get:
            with self.assertRaisesRegex(RuntimeError, 'Unexpected AWS account'):
                module.seed()
            get.assert_not_called()

    def test_existing_pair_is_preserved_without_source_read_or_write(self):
        with patch.object(module, 'aws', return_value={'Account': '483175530259'}) as aws, patch.object(module, 'parameter', return_value={'Parameter': {'Value': 'existing'}}) as get, patch('builtins.print'):
            module.seed()
            aws.assert_called_once_with('sts', 'get-caller-identity')
            self.assertEqual(get.call_count, 5)

    def test_partial_pair_fails_before_copy(self):
        with patch.object(module, 'aws', return_value={'Account': '483175530259'}) as aws, patch.object(module, 'parameter', side_effect=[{'Parameter': {}}] * 4 + [None]):
            with self.assertRaisesRegex(RuntimeError, 'Partial production RunPod pair'):
                module.seed()
            aws.assert_called_once_with('sts', 'get-caller-identity')


if __name__ == '__main__':
    unittest.main()
