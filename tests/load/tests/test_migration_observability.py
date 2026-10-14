import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('sampler', Path(__file__).parents[3] / 'monitoring/v2-staging/proxysql-metrics.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class ProxyMetricsTest(unittest.TestCase):
    def test_read_only_sampling_has_explicit_units_and_no_password(self):
        responses = [[['10','172.28.0.1','3306','ONLINE','2','3','1','99']], [['10']],
                     [['Active_Transactions','1'],['Server_Connections_delayed','7']], [['4','1500']]]
        with patch.object(module,'query',side_effect=responses) as query, patch.object(module.subprocess,'check_output',side_effect=['container','jdbc:mysql://proxysql:6033/yeodam_v1_source']):
            lines = '\n'.join(module.sample('secret-that-must-not-be-exported'))
        self.assertIn('unassigned_state_max_seconds 1.5',lines)
        self.assertIn('connection_delays_total 7',lines)
        self.assertIn('yeodam_backend_uses_proxysql 1',lines)
        self.assertNotIn('secret-that-must-not-be-exported',lines)
        self.assertTrue(all(call.args[0].startswith('SELECT ') and '_reset' not in call.args[0] for call in query.call_args_list))

    def test_mixed_routing_is_rejected_instead_of_reporting_a_single_writer(self):
        with patch.object(module,'query',side_effect=[[],[['10'],['20']]]):
            with self.assertRaises(ValueError): module.sample('private')
