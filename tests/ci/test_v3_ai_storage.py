import copy,importlib.util,json,os,stat,subprocess,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def module(name,file):
    s=importlib.util.spec_from_file_location(name,ROOT/'scripts'/file);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
storage=module('ai_storage','check-v3-ai-storage.py');prepare=module('ai_prepare','prepare-v3-ai-storage-secrets.py');probe=module('ai_probe','verify-v3-ai-storage.py');guard=module('ai_guard','check-v3-ai-storage-plan.py')
class StorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.docs=storage.platform.load_documents(storage.render(True))
    def reject(self,change):
        docs=copy.deepcopy(self.docs);change(docs)
        with self.assertRaises((ValueError,KeyError,StopIteration)):storage.validate(docs,True)
    def state(self,docs):return next(d for d in docs if d['kind']=='StatefulSet')
    def test_default_no_pods_or_disk(self):storage.validate(storage.platform.load_documents(storage.render()),False)
    def test_enabled_minimum(self):storage.validate(self.docs,True)
    def test_public_exposure_rejected(self):self.reject(lambda ds:next(d for d in ds if d['kind']=='Service')['spec'].update(type='LoadBalancer'))
    def test_network_widening_rejected(self):self.reject(lambda ds:next(d for d in ds if d['kind']=='NetworkPolicy')['spec'].update(ingress=[{}]))
    def test_cross_namespace_rejected(self):self.reject(lambda ds:self.state(ds)['metadata'].update(namespace='redis'))
    def test_replica_increase_rejected(self):self.reject(lambda ds:self.state(ds)['spec'].update(replicas=2))
    def test_pvc_delete_rejected(self):self.reject(lambda ds:self.state(ds)['spec'].update(persistentVolumeClaimRetentionPolicy={'whenDeleted':'Delete','whenScaled':'Delete'}))
    def test_storage_expansion_rejected(self):self.reject(lambda ds:self.state(ds)['spec']['volumeClaimTemplates'][0]['spec']['resources']['requests'].update(storage='50Gi'))
    def test_resource_increase_rejected(self):self.reject(lambda ds:self.state(ds)['spec']['template']['spec']['containers'][0]['resources']['limits'].update(memory='8Gi'))
    def test_wrong_node_rejected(self):self.reject(lambda ds:self.state(ds)['spec']['template']['spec'].update(nodeSelector={'yeodam.io/role':'system'}))
    def test_secret_payload_rejected(self):self.reject(lambda ds:ds.append({'kind':'Secret','metadata':{'name':'bad'}}))
    def test_plaintext_probe_rejected(self):self.reject(lambda ds:next(d for d in ds if d['kind']=='StatefulSet' and d['metadata']['name']=='qdrant')['spec']['template']['spec']['containers'][0]['readinessProbe']['httpGet'].update(scheme='HTTP'))
    def test_inline_key_rejected(self):self.reject(lambda ds:next(d for d in ds if d['kind']=='StatefulSet' and d['metadata']['name']=='qdrant')['spec']['template']['spec']['containers'][0].update(env=[{'name':'QDRANT__SERVICE__API_KEY','value':'bad'}]))
    def test_privileged_rejected(self):self.reject(lambda ds:self.state(ds)['spec']['template']['spec']['containers'][0]['securityContext'].update(privileged=True))
    def test_unknown_values_rejected(self):
        r=subprocess.run([os.environ['V3_HELM'],'template','check',str(storage.CHART),'--kube-version','1.36.0','--set','password=forbidden'],capture_output=True);self.assertNotEqual(r.returncode,0)
    def test_generated_sources_private_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as t:
            d=Path(t)/'generated';prepare.prepare(d)
            for name in ['ai-redis','qdrant']:
                f=d/name/'source.json';self.assertEqual(stat.S_IMODE(f.stat().st_mode),0o600)
                value=json.loads(f.read_text());self.assertIn('tlsKey',value)
                subprocess.run(['openssl','verify','-x509_strict','-CAfile',str(d/name/'ca.crt'),'-verify_hostname',name+'.'+name+'.svc.cluster.local',str(d/name/'tls.crt')],check=True,capture_output=True)
            with self.assertRaises(FileExistsError):prepare.prepare(d)
    def test_generate_inside_checkout_rejected(self):
        with self.assertRaises(ValueError):prepare.prepare(ROOT/'unsafe-generated')
    def test_probe_production_override_rejected(self):
        with self.assertRaises(ValueError):probe.Client('qdrant',Path('/tmp'),'production.example',6333)
    def test_probe_cleanup_name_guard(self):
        class Fake:pass
        with self.assertRaises(ValueError):probe.exercise(Fake(),'cleanup','production')
    def test_resp_wire_frame(self):
        class Sock:
            def sendall(self,raw):self.raw=raw
        p=probe.Client.__new__(probe.Client);p.sock=Sock();p.read=lambda:'PONG';self.assertEqual(p.command('PING'),'PONG');self.assertEqual(p.sock.raw,bytes([42,49,13,10,36,52,13,10,80,73,78,71,13,10]))
    def test_plan_noop_allowed(self):self.assertEqual(guard.validate({'complete':True}),{'creates':0,'updates':0,'deletes':0})
    def test_plan_delete_rejected(self):
        with self.assertRaises(ValueError):guard.validate({'complete':True,'resource_changes':[{'mode':'managed','address':'aws_iam_role.reader["mysql"]','change':{'actions':['delete']}}]})
    def fixture_plan(self):
        old={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['sts:AssumeRole','sts:TagSession'],'Resource':['arn:aws:iam::483175530259:role/yeodam-v3-foundation-eso-auth']}]}
        after=copy.deepcopy(old);after['Statement'][0]['Resource']+=sorted(guard.ARNS)
        changes=[{'mode':'managed','address':guard.UPDATE,'change':{'actions':['update'],'before':{'policy':json.dumps(old)},'after':{'policy':json.dumps(after)}}}]
        for n in ['ai-redis','qdrant']:
            trust={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'AWS':'arn:aws:iam::483175530259:role/yeodam-v3-foundation-eso-controller'},'Action':['sts:AssumeRole','sts:TagSession']}]}
            policy={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['secretsmanager:GetSecretValue','secretsmanager:DescribeSecret'],'Resource':['arn:aws:secretsmanager:ap-northeast-2:483175530259:secret:yeodam/v3/foundation/'+n+'/*']}]}
            changes+=[{'mode':'managed','address':'aws_iam_role.reader["'+n+'"]','change':{'actions':['create'],'after':{'name':'yeodam-v3-foundation-eso-'+n,'assume_role_policy':json.dumps(trust)}}},{'mode':'managed','address':'aws_iam_role_policy.read["'+n+'"]','change':{'actions':['create'],'after':{'policy':json.dumps(policy)}}}]
        return {'complete':True,'resource_changes':changes}
    def test_additive_reader_plan(self):self.assertEqual(guard.validate(self.fixture_plan()),{'creates':4,'updates':1,'deletes':0})
    def test_reader_prefix_widening_rejected(self):
        p=self.fixture_plan();p['resource_changes'][-1]['change']['after']['policy']=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['secretsmanager:GetSecretValue','secretsmanager:DescribeSecret'],'Resource':['*']}]})
        with self.assertRaises(ValueError):guard.validate(p)
    def test_existing_reader_removal_rejected(self):
        p=self.fixture_plan();v=json.loads(p['resource_changes'][0]['change']['after']['policy']);v['Statement'][0]['Resource']=sorted(guard.ARNS);p['resource_changes'][0]['change']['after']['policy']=json.dumps(v)
        with self.assertRaises(ValueError):guard.validate(p)
    def test_aof_replay_disabled_default(self):
        script=(storage.CHART/'files/redis/start.sh').read_text();self.assertIn('user default reset off ~* &* +@all',script)
    def test_qdrant_plaintext_auth_error(self):
        from unittest.mock import patch
        class Response:
            status=403
            def read(self):return b'API key required'
        class Connection:
            def __init__(self,*a,**kw):pass
            def request(self,*a,**kw):pass
            def getresponse(self):return Response()
            def close(self):pass
        c=probe.Client.__new__(probe.Client);c.host='qdrant.qdrant.svc.cluster.local';c.port=6333;c.context=None;c.socket=lambda:None
        with patch.object(probe.http.client,'HTTPSConnection',Connection):self.assertEqual(c.http('GET','/collections',key=False),(403,{}))
if __name__=='__main__':unittest.main()
