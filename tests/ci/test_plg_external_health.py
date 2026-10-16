import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('plg_health',ROOT/'monitoring/v2-staging/publish-plg-health.py')
health = importlib.util.module_from_spec(spec); spec.loader.exec_module(health)

class PLGExternalHealthTest(unittest.TestCase):
    def responses(self, url):
        if '/api/health' in url: return b'{"database":"ok"}'
        if '/api/v1/query' in url: return b'{"status":"success","data":{"result":[{"value":[0,"1"]}]}}'
        return b'ready'

    def test_all_ready_publishes_one(self):
        with patch.object(health,'read',side_effect=self.responses),patch.object(health.subprocess,'run') as command:
            self.assertEqual(health.publish('i-00000000000000000', health.check()),1)
        payload=json.loads(command.call_args.args[0][-1])
        self.assertEqual(payload[0]['Value'],1)
        self.assertEqual(payload[0]['Dimensions'],[{'Name':'InstanceId','Value':'i-00000000000000000'}])

    def test_grafana_database_failure_publishes_zero(self):
        def response(url):return b'{"database":"failing"}' if '/api/health' in url else self.responses(url)
        with patch.object(health,'read',side_effect=response),patch.object(health.subprocess,'run') as command:
            self.assertEqual(health.publish('i-00000000000000000',health.check()),0)
        self.assertEqual(json.loads(command.call_args.args[0][-1])[0]['Value'],0)

    def test_http_timeout_does_not_skip_failure_publication(self):
        with patch.object(health,'read',side_effect=OSError),patch.object(health.subprocess,'run') as command:
            self.assertEqual(health.publish('i-00000000000000000',health.check()),0)
        command.assert_called_once()

    def test_stale_or_missing_self_scrape_is_not_ready(self):
        for result in [[],[{'value':[0,'0']}],[{'value':[0,'NaN']}]]:
            def response(url):
                if '/api/v1/query' in url:return json.dumps({'status':'success','data':{'result':result}}).encode()
                return self.responses(url)
            with patch.object(health,'read',side_effect=response):self.assertFalse(health.check()['prometheus'])

if __name__=='__main__':unittest.main()
