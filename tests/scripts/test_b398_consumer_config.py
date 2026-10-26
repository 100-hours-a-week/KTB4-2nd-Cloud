import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('app398', ROOT/'scripts/b-integration376-app.py')
app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)

class ConsumerConfigurationTests(unittest.TestCase):
    def check(self, configurations, run='owned'):
        connection=MagicMock();response=connection.getresponse.return_value;response.status=200
        response.read.side_effect=[value.encode() for value in configurations]
        with patch.object(app,'inspect',return_value={'Config':{'Labels':{'yeodam.run':run}}}),patch.object(app,'Connection',return_value=connection):
            app.consumer_config_ready('owned')

    def test_both_generated_listeners_must_attest_before_input(self):
        self.check(['migration_trip_consumer_config max_poll_records=32 enable_auto_commit=false concurrency=4',
                    'migration_trip_consumer_config max_poll_records=32 enable_auto_commit=false concurrency=8'])
        for wrong in ('', 'migration_trip_consumer_config max_poll_records=500 enable_auto_commit=false concurrency=4'):
            with self.assertRaises(RuntimeError):self.check([wrong])

    def test_another_runs_container_cannot_attest(self):
        with self.assertRaises(RuntimeError):self.check([],run='another')

if __name__=='__main__':unittest.main()
