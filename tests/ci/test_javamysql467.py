import importlib.util
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock
ROOT=Path(__file__).parents[2]
def load(name,file):
 s=importlib.util.spec_from_file_location(name,ROOT/file);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
h=load('host467','scripts/javamysql467-host.py');o=load('overlay467','scripts/javamysql467-overlay.py');d=load('driver467','scripts/diagnose-javamysql467.py')
class DiagnosticContracts(unittest.TestCase):
 def test_jfr_rejects_secret_capture_even_when_template_enables_it(self):
  original='<configuration version="2.0">'+''.join('<event name="'+n+'"><setting name="enabled">true</setting></event>' for n in h.DENIED_EVENTS)+'</configuration>'
  root=ET.fromstring(h.private_jfc(original))
  for n in h.DENIED_EVENTS:self.assertEqual([s.text for e in root.findall('event') if e.get('name')==n for s in e.findall("setting[@name='enabled']")],['false'])
 def test_missing_sensitive_events_are_explicitly_disabled(self):
  root=ET.fromstring(h.private_jfc('<configuration version="2.0"/>'))
  self.assertEqual({e.get('name') for e in root.findall('event')},h.DENIED_EVENTS)
 def test_overlay_does_not_change_heap_or_cpu_or_image(self):
  needle="    payload={'Image':IMAGE,'Env':[k+'='+v for k,v in env.items()],'HostConfig':hc,"
  result=o.app(needle)
  self.assertIn(needle,result);self.assertNotIn('Xmx',result);self.assertNotIn('ActiveProcessorCount',result);self.assertNotIn('jmxremote',result);self.assertIn('maxsize=64m',result)
 def test_duration_changes_are_explicit_not_global(self):
  text="'K6_TRIP_DURATION_SECONDS':'900'\nother=900"
  self.assertEqual(o.generator(text),"'K6_TRIP_DURATION_SECONDS':'180'\nother=900")
  with self.assertRaises(ValueError):o.generator('different')
 def test_pre_pool_http_check_exists(self):
  m=Mock();d.source_http_ready(m);self.assertIn('/api/auth/csrf',m.raw.call_args.args[1][0]);self.assertEqual(m.raw.call_args.args[0],'source')
 def test_diagnostic_prefix_preserves_parent_preparation_but_never_arms_cutover(self):
  r=load('run467','scripts/javamysql467-run.py')
  text="def execute(case,index):\n    state={'duration':900}\n    load_id=phase('generator','b-integration376-generator.py',run,'start','--rate',str(rate),sync=False)\n    checkpoint('load_started')\n    kafka_standby=raw('kafka',[])\n    fence_id=raw('source',[])\n"
  result=r.preparation_source(text)
  self.assertIn("'duration':180",result);self.assertIn("'before-input'",result)
  self.assertNotIn('kafka_standby=',result);self.assertNotIn('fence_id=',result)
  with self.assertRaises(ValueError):r.preparation_source('different')
 def test_scenario_preserves_load_allocation_and_timeouts(self):
  text="duration !== 900\nand 900s\nduration: '900s'\nmaxDuration: '1005s'\nexec.scenario.startTime + 900000\npreAllocatedVUs: vus, maxVUs: maxVus, gracefulStop: '105s'\nhttpTimeout:100"
  result=o.scenario(text)
  self.assertIn("duration: '180s'",result);self.assertIn("maxDuration: '285s'",result)
  self.assertIn("gracefulStop: '105s'",result);self.assertIn('httpTimeout:100',result)
  self.assertIn('preAllocatedVUs: vus, maxVUs: maxVus',result)
  with self.assertRaises(ValueError):o.scenario('different')
 def test_counter_reset_is_not_ranked_as_a_speedup(self):
  an=load('analysis467','scripts/analyze-javamysql467.py')
  def cp(n):return {'startedAt':n,'digests':[{'SCHEMA_NAME':'app','DIGEST':'one','DIGEST_TEXT':'COMMIT','COUNT_STAR':str(n),'SUM_TIMER_WAIT':str(n*1000),'SUM_CPU_TIME':'0','SUM_LOCK_TIME':'0','SUM_ROWS_EXAMINED':'0','SUM_ROWS_SENT':'0'}]}
  result=an.digest_delta(cp(20),cp(10));self.assertEqual(result['counterResetDigests'],[['app','one']]);self.assertEqual(result['allChangedDigests'],[])
 def test_cpu_zero_is_unknown_not_zero_cost(self):
  an=load('analysis467b','scripts/analyze-javamysql467.py')
  before={'startedAt':0,'digests':[]};after={'startedAt':1,'digests':[{'SCHEMA_NAME':'app','DIGEST':'one','DIGEST_TEXT':'COMMIT','COUNT_STAR':'2','SUM_TIMER_WAIT':'2000000000','SUM_CPU_TIME':'0','SUM_LOCK_TIME':'0','SUM_ROWS_EXAMINED':'0','SUM_ROWS_SENT':'0'}]}
  result=an.digest_delta(before,after);self.assertFalse(result['cpuCounterNonzero']);self.assertEqual(result['categories']['transaction_end']['meanElapsedMs'],1)
 def test_reader_mount_flags_do_not_become_part_of_container_paths(self):
  code=(ROOT/'scripts/javamysql467-close.py').read_text()
  self.assertIn('target=/run/reader,readonly',code);self.assertIn('target=/run/trace,readonly',code)
  self.assertNotIn('target=/run/reader:ro',code);self.assertNotIn('target=/run/trace:ro',code)
if __name__=='__main__':unittest.main()
