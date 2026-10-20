"""Production boundaries and exporter failure behavior, without AWS or business writes."""
import importlib.util,json,unittest,urllib.error
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
P=ROOT/'monitoring/v2-production'
def load(name):
 spec=importlib.util.spec_from_file_location(name,P/(name+'.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
class Contract(unittest.TestCase):
 def test_private_production_boundary(self):
  t=(ROOT/'terraform/v2/production/observability.tf').read_text()
  self.assertNotIn('0.0.0.0/0',t)
  self.assertIn('referenced_security_group_id',t)
  self.assertNotIn('6379',t);self.assertNotIn('3306',t)
  self.assertIn('127.0.0.1:3000:3000',(P/'plg-compose.yml').read_text())
 def test_missing_connector_not_healthy_server(self):
  m=load('probe-exporter')
  with patch.object(m.urllib.request,'urlopen',side_effect=urllib.error.HTTPError('x',404,'missing',{},None)):
   result=m.collect('connect')
  self.assertIn('yeodam_connect_expected_connector_running 0',result)
  self.assertIn('yeodam_observer_success{probe="connect"} 1',result)
 def test_failed_db_probe_has_no_fake_zero_backlog(self):
  m=load('probe-exporter')
  with patch.object(m.Path,'read_text',side_effect=OSError):result=m.collect('mysql')
  self.assertIn('yeodam_observer_success{probe="mysql"} 0',result)
  self.assertNotIn('yeodam_outbox_pending',result)
 def test_secrets_not_in_host_log_output(self):
  m=load('ship-host-logs');s=m.redact('Authorization: Bearer abc.def URL?X-Amz-Signature=secret&ok=1')
  self.assertNotIn('abc.def',s);self.assertNotIn('=secret',s)
 def test_shared_gpu_isolated_agent(self):
  s=(P/'install-ai-gpu-metrics.sh').read_text()
  self.assertIn('production-gpu-sample',s)
  self.assertNotIn('docker.sock',(P/'ai-gpu-alloy-compose.yml').read_text())
  self.assertIn(':6335',(P/'ai-gpu.alloy').read_text())
  self.assertIn(':12346',(P/'discover-gpu-state.py').read_text())
 def test_low_cardinality(self):
  m=load('probe-exporter')
  self.assertEqual(len(m.TABLES),3)
  self.assertNotIn('trip_id=',(P/'probe-exporter.py').read_text())
  config=json.loads((P/'alerts.json.tftpl').read_text())
  self.assertEqual(len({x['uid'] for x in config}),len(config))
if __name__=='__main__':unittest.main()
