import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('backend_parameters', Path(__file__).parents[2] / 'scripts/seed-v2-production-backend-parameters.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BackendParameterBoundary(unittest.TestCase):
    def test_wrong_account_stops_before_parameter_access(self):
        with patch.object(module, 'aws', return_value={'Account': '000000000000'}), patch.object(module, 'parameter') as read:
            with self.assertRaisesRegex(RuntimeError, 'Unexpected AWS account'):
                module.seed()
            read.assert_not_called()

    def test_existing_different_auth_key_is_never_overwritten(self):
        def read(path):
            return {'Type': 'SecureString', 'Value': ('new' if path.startswith('/yeodam/v1/') else 'old') * 20}
        with patch.object(module, 'aws', return_value={'Account': '483175530259'}) as aws, patch.object(module, 'parameter', side_effect=read):
            with self.assertRaisesRegex(RuntimeError, 'no overwrite: JWT_SECRET'):
                module.seed()
            aws.assert_called_once_with('sts', 'get-caller-identity')

    def test_matching_parameters_are_preserved_without_writes(self):
        with patch.object(module, 'aws', return_value={'Account': '483175530259'}) as aws, patch.object(module, 'parameter', return_value={'Type': 'SecureString', 'Value': 'x' * 64}), patch('builtins.print'):
            module.seed()
            aws.assert_called_once_with('sts', 'get-caller-identity')


if __name__ == '__main__':
    unittest.main()
