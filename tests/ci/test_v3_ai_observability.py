import copy,importlib.util,json,os,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
from v3_ai_observability import transition,prepare,readers
spec=importlib.util.spec_from_file_location('ai',ROOT/'scripts/check-v3-ai.py');ai=importlib.util.module_from_spec(spec);spec.loader.exec_module(ai)

class Observability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v=ai.fixture();cls.v['apmWorkloads']=['api','runner']
    def render(self,v):
        docs=ai.platform.load_documents(ai.render(v));ai.validate(docs,v);return docs
    def candidate(self,v=None):
        return {'values':{'ai':copy.deepcopy(v or self.v)},'components':{'ai-app':{'image':self.v['image']}}}
    def test_reader_then_writer_changes_only_reviewed_ai_resources(self):
        source=self.candidate();reader=prepare(source,'reader');writer=prepare(reader,'writer')
        before=self.render(source['values']['ai']);after=self.render(reader['values']['ai']);last=self.render(writer['values']['ai'])
        def index(ds):return {(d['kind'],d['metadata']['name']):d for d in ds}
        a,b,c=index(before),index(after),index(last)
        self.assertEqual(set(a),set(b));self.assertEqual(set(b),set(c))
        self.assertEqual({k for k in a if a[k]!=b[k]},{('Deployment','ai-api'),('Deployment','ai-runner'),('ConfigMap','ai-api-runtime'),('ConfigMap','ai-runner-runtime'),('ExternalSecret','ai-runtime')})
        self.assertEqual({k for k in b if b[k]!=c[k]},{('Deployment','ai-api'),('Deployment','ai-runner'),('ConfigMap','ai-api-runtime'),('ConfigMap','ai-runner-runtime')})
        for key in a:
            if key[0]=='Deployment':
                x=a[key]['spec']['template']['spec']['containers'][0];y=b[key]['spec']['template']['spec']['containers'][0]
                self.assertEqual((x['image'],x['resources'],x['command']),(y['image'],y['resources'],y['command']))
        transition(source,reader);transition(reader,writer)
    def test_initial_writer_skipping_reader_or_changed_image_is_rejected(self):
        old=self.candidate();reader=prepare(old,'reader');writer=prepare(reader,'writer')
        for prior in [None,old]:
            with self.assertRaises(ValueError):transition(prior,writer)
        bad=copy.deepcopy(writer);bad['values']['ai']['image']='new-image'
        with self.assertRaises(ValueError):transition(reader,bad)
    def test_written_records_block_old_strict_rollback_even_after_switch_off(self):
        reader=prepare(self.candidate(),'reader');writer=prepare(reader,'writer');off=prepare(writer,'reader')
        transition(writer,off)
        for old in [self.candidate(),reader]:
            with self.assertRaises(ValueError):transition(off,old)
    def test_write_without_floor_or_without_both_instrumented_readers_blocks(self):
        reader=prepare(self.candidate(),'reader')['values']['ai']
        for patch in [{'traceContextWriteEnabled':True},{'traceContextEnabled':False},{'workloads':{'api':True,'runner':False}},{'apmWorkloads':['api']}]:
            v=copy.deepcopy(reader);v.update(patch)
            with self.assertRaises(Exception):self.render(v)
    def test_langfuse_keys_are_reference_only_and_base_url_fixed(self):
        v=prepare(self.candidate(),'reader')['values']['ai'];docs=self.render(v)
        raw=json.dumps(docs);self.assertNotIn('sk-lf-',raw);self.assertNotIn('pk-lf-',raw)
        es=next(d for d in docs if d['kind']=='ExternalSecret');keys=[x['secretKey']for x in es['spec']['data']]
        self.assertEqual(keys[-2:],['LANGFUSE_PUBLIC_KEY','LANGFUSE_SECRET_KEY'])
        for d in docs:
            if d['kind']=='ConfigMap'and d['metadata']['name']in ['ai-api-runtime','ai-runner-runtime']:
                self.assertEqual(d['data']['LANGFUSE_BASE_URL'],'https://cloud.langfuse.com');self.assertEqual(d['data']['JOBS_TRACE_CONTEXT_WRITE'],'false')
        wrong=copy.deepcopy(docs);next(d for d in wrong if d['kind']=='ConfigMap'and d['metadata']['name']=='ai-api-runtime')['data']['LANGFUSE_SECRET_KEY']='synthetic-forbidden'
        with self.assertRaises(ValueError):ai.validate(wrong,v)
    def readers(self):
        v=prepare(self.candidate(),'reader');image=v['components']['ai-app']['image'];deploy=[];pods=[]
        for name in ['ai-api','ai-runner']:
            container={'image':image,'envFrom':[{'configMapRef':{'name':name+'-runtime'}}]}
            deploy.append({'metadata':{'name':name,'namespace':'ai','generation':2},'spec':{'replicas':1,'template':{'spec':{'containers':[container]}}},'status':{'observedGeneration':2,'replicas':1,'updatedReplicas':1,'readyReplicas':1}})
            pods.append({'metadata':{'name':name+'-fixture','namespace':'ai','uid':name,'labels':{'app.kubernetes.io/instance':name}},'spec':{'containers':[container]},'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}})
        return deploy,pods,v
    def test_reader_review_rejects_old_terminating_not_ready_or_env_override(self):
        ds,ps,c=self.readers();self.assertEqual(len(readers(ds,ps,c)),2)
        for mutation in [lambda d,p:p.append(copy.deepcopy(p[0])),lambda d,p:p[0]['metadata'].update(deletionTimestamp='now'),lambda d,p:p[0]['spec']['containers'][0].update(image='old'),lambda d,p:p[0]['status'].update(conditions=[]),lambda d,p:p[0]['spec']['containers'][0].update(env=[{'name':'JOBS_TRACE_CONTEXT_WRITE','value':'true'}]),lambda d,p:d[0]['status'].update(observedGeneration=1)]:
            d,p=copy.deepcopy(ds),copy.deepcopy(ps);mutation(d,p)
            with self.assertRaises(ValueError):readers(d,p,c)

if __name__=='__main__':unittest.main()
