import copy,importlib.util,json,os,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
from v3_apm_evidence import integrity,observe,checkpoint,metrics
from v3_apm_contract import java_env
import v3_release_bundle as bundle

def load(name,path):
 s=importlib.util.spec_from_file_location(name,ROOT/path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
class Reliability(unittest.TestCase):
 def healthy(self):return {'up':[1,1],'age':[1,1],'started':[100,101],'sent':[20],'capacity':[2097152],'queue':[0]}
 def test_integrity_distinguishes_loss_unknown_reset_and_pending(self):
  good=self.healthy();self.assertEqual(integrity(good,good),'no_observed_loss')
  for key,value,expected in [('rejected',[1],'collection_failure_observed'),('discarded',[1],'collection_failure_observed'),('up',[1,0],'unknown'),('age',[1,60],'unknown'),('started',[100,102],'counter_reset_or_missing'),('queue',[1],'pending_export'),('capacity',[],'unknown')]:
   with self.subTest(key=key):
    after=copy.deepcopy(good);after[key]=value;self.assertEqual(integrity(good,after),expected)
  before=dict(good,rejected=[3]);after=dict(good,rejected=[2]);self.assertEqual(integrity(before,after),'counter_reset_or_missing')
 def test_partial_trace_survives_read_timeout(self):
  calls=[{'resourceSpans':[]},TimeoutError()];clock=[0]
  def read(*args):
   value=calls.pop(0)
   if isinstance(value,Exception):raise value
   return value
  def sleep(s):clock[0]+=s
  with tempfile.TemporaryDirectory()as folder:
   os.chmod(folder,0o700);path=Path(folder)/'evidence.json'
   result=observe('1'*32,'http://localhost','http://localhost',path,deadline=1,interval=1,read=read,probe=lambda b:self.healthy(),sleep=sleep,clock=lambda:clock[0])
   self.assertFalse(result['traceEvidenceReady']);self.assertEqual(len(result['errors']),1)
   self.assertEqual(result['trace'],json.loads(path.read_text())['trace']);self.assertEqual(path.stat().st_mode&0o777,0o600)
   with self.assertRaises(ValueError):observe('1'*32,'x','y',path)
 def test_remote_metrics_rejected_before_network(self):
  for url in ['https://example.com','http://127.0.0.1/x','http://user@localhost','http://localhost?secret=1']:
   with self.assertRaises(ValueError):metrics(url)
 def test_native_driver_reduction_preserves_url_tls_and_jdbc(self):
  config={'namespace':'photo','configMapData':{'MYSQL_URL':'jdbc:mysql://mysql:3306/yeodam?sslMode=VERIFY_IDENTITY','RELEASE':'fixed','JAVA_TOOL_OPTIONS':'-Xmx384m'}}
  old={x['name']:x['value']for x in java_env('photo-converter',config)}
  new={x['name']:x['value']for x in java_env('photo-converter',config,True)}
  self.assertNotIn('MYSQL_URL',old);self.assertEqual(new['MYSQL_URL'],config['configMapData']['MYSQL_URL']+'&openTelemetry=DISABLED')
  self.assertNotIn('OTEL_INSTRUMENTATION_JDBC_ENABLED',new);self.assertEqual(new['OTEL_INSTRUMENTATION_HIBERNATE_ENABLED'],'false')
  self.assertEqual(old['JAVA_TOOL_OPTIONS'],new['JAVA_TOOL_OPTIONS']);self.assertEqual(new['OTEL_BSP_MAX_QUEUE_SIZE'],'512')
  config['configMapData']['MYSQL_URL']+='&OpenTelemetry=PREFERRED'
  with self.assertRaises(ValueError):java_env('photo-converter',config,True)
 def test_dashboard_file_is_mounted_on_existing_grafana(self):
  chart=load('metrics_reliable','scripts/check-v3-metrics.py');docs=chart.render(apm=True);chart.validate(docs)
  grafana=next(d for d in docs if d['kind']=='Deployment'and d['metadata']['name']=='v3-grafana')
  mounts=grafana['spec']['template']['spec']['containers'][0]['volumeMounts']
  self.assertIn({'name':'config','mountPath':'/etc/grafana/dashboards/apm.json','subPath':'apm.json','readOnly':True},mounts)
 def test_opt_in_changes_only_telemetry_on_existing_seven_apps(self):
  prep=load('reliable','scripts/prepare-v3-apm-reliability.py')
  source=json.loads((ROOT/'deploy/v3/releases/candidate.json').read_text());original=copy.deepcopy(source);new=prep.prepare(source)
  self.assertEqual(source,original)
  charts={'charts/'+c+'/'+str(p.relative_to(ROOT/'deploy/v3'/c)):p.read_bytes()for c in ['apps','ai-apps','service-access']for p in (ROOT/'deploy/v3'/c).rglob('*')if p.is_file()}
  def idx(v):return {(d['kind'],d['metadata'].get('namespace'),d['metadata']['name']):d for d in bundle.render_profiles(charts,v)}
  before=idx(source);after=idx(new);self.assertEqual(set(before),set(after))
  changed=[k for k in before if before[k]!=after[k]]
  self.assertEqual({k[2]for k in changed if k[0]=='Deployment'},{'auth-api','trip-api','photo-api','photo-converter','trip-result-worker','ai-api','ai-runner'})
  self.assertEqual([k[0]for k in changed].count('ConfigMap'),0)
  for k in before:
   if k[0]!='Deployment':continue
   a=before[k]['spec'];b=after[k]['spec'];self.assertEqual(a.get('replicas'),b.get('replicas'))
   x=a['template']['spec'];y=b['template']['spec']
   for field in ['nodeSelector','tolerations','affinity','serviceAccountName','terminationGracePeriodSeconds']:self.assertEqual(x.get(field),y.get(field))
   for c,d in zip(x['containers'],y['containers']):
    for field in ['image','resources','command','readinessProbe','livenessProbe','startupProbe','volumeMounts']:self.assertEqual(c.get(field),d.get(field))
if __name__=='__main__':unittest.main()
