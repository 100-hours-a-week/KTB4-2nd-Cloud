#!/usr/bin/env python3
"""Offline minimum AI runtime/connection contracts. No cluster, image pull or model call."""
import argparse,copy,hashlib,importlib.util,json,os,re,subprocess,tempfile
from collections import Counter
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1];CHART=ROOT/'deploy/v3/ai-apps';CONTRACT=json.loads((CHART/'files/contract.json').read_text());POLICIES=json.loads((CHART/'files/policies.json').read_text())

apm_spec=importlib.util.spec_from_file_location('apm_contract',ROOT/'scripts/v3_apm_contract.py');apm_contract=importlib.util.module_from_spec(apm_spec);apm_spec.loader.exec_module(apm_contract)
spec=importlib.util.spec_from_file_location('platform',ROOT/'scripts/check-v3-platform.py');platform=importlib.util.module_from_spec(spec);spec.loader.exec_module(platform);require=platform.require

def fixture():
    with tempfile.TemporaryDirectory(prefix='ai-ca-fixture-') as d:
        ca=Path(d)/'ca.crt';key=Path(d)/'ca.key';subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=synthetic-ci-only','-keyout',str(key),'-out',str(ca)],check=True,capture_output=True);public=ca.read_text()
    return {'accessEnabled':True,'workloads':{'api':True,'runner':True},'image':'ghcr.io/100-hours-a-week/yeodam-ai-app@sha256:'+'a'*64,'source':'b'*40,'configurationRevision':'c'*64,'review':{'sourceCompatible':True,'imageReviewed':True,'connectionsReviewed':True},'trust':{k:public for k in ['redis','qdrant','kafka']},'privateModelPeers':[]}
def render(values=None):
    args=[os.environ['V3_HELM'],'template','yeodam-v3-ai-apps',str(CHART),'-n','ai','--kube-version','1.36.0']
    return subprocess.check_output(args+(['-f','-'] if values else []),input=yaml.safe_dump(values) if values else None,text=True,stderr=subprocess.PIPE)
def _validate(docs,v,contract,runtime_scripts,policy_sources,legacy=False):
    require(contract['workloads']['runner'].get('environment',{}).get('PYTHONPATH')=='/app','File-based runner must resolve the /app package')
    enabled=v['accessEnabled'];roles=[r for r,x in v['workloads'].items() if x];expected={}
    if enabled:
        expected={'ConfigMap':4,'ServiceAccount':2,'ExternalSecret':1,'NetworkPolicy':7+bool(v['privateModelPeers'])}
        if roles:expected['Deployment']=len(roles)
        if v['workloads']['api']:expected['Service']=1
    require(dict(Counter(d['kind'] for d in docs))==expected,'Exact inventory; no Namespace/PVC/Secret/Ingress')
    if not enabled:return
    apm_roles=v.get('apmWorkloads',[])
    trace_enabled=v.get('traceContextEnabled',False);write=v.get('traceContextWriteEnabled',False)
    ever=v.get('traceContextWriteEverEnabled',False);langfuse=v.get('langfuseEnabled',False)
    obs=json.loads((CHART/'files/observability.json').read_text())
    require(not (write or ever or langfuse) or trace_enabled,'Compatible reader required')
    require(not write or ever,'Record sticky reader compatibility floor')
    require(not trace_enabled or (set(roles)=={'api','runner'} and set(apm_roles)=={'api','runner'}),'Both instrumented readers required')
    if apm_roles:
        runtime_scripts=dict(runtime_scripts,sitecustomize=(CHART/'files/apm/sitecustomize.py').read_text())
        runtime_scripts['sitecustomize.py']=runtime_scripts.pop('sitecustomize')
    require(not roles or all(v['review'].values()),'All source/image/model reviews required')
    identities=[(d['kind'],d['metadata']['namespace'],d['metadata']['name']) for d in docs];require(len(identities)==len(set(identities)),'Duplicate resources')
    expectedPolicies={(p['namespace'],p['name']):p['spec'] for p in policy_sources}
    if v['privateModelPeers']:expectedPolicies[('ai','allow-ai-reviewed-private-models')]={'podSelector':{'matchLabels':{'yeodam.io/ai-runtime':'true','yeodam.io/stage':'v3'}},'policyTypes':['Egress'],'egress':[{'to':[{'ipBlock':{'cidr':p['cidr']}}],'ports':[{'protocol':'TCP','port':p['port']}]} for p in v['privateModelPeers']]}
    for d in docs:
        n=d['metadata']['namespace'];name=d['metadata']['name'];kind=d['kind']
        if kind=='NetworkPolicy':require((n,name) in expectedPolicies and d['spec']==expectedPolicies[(n,name)],'Exact cross-namespace peers and ports');continue
        require(n=='ai','Only policies may cross the AI namespace')
        if kind=='ConfigMap':
            if name=='ai-trust':require(d['data']=={k+'.crt':v['trust'][k] for k in ['redis','qdrant','kafka']},'Public CA references only')
            elif name=='ai-runtime-scripts':require(d['data']==runtime_scripts,'Runtime scripts pinned')
            else:
                role='api' if name=='ai-api-runtime' else 'runner'
                environment=dict(contract['commonEnvironment'],RELEASE=v['source'],**contract['workloads'][role].get('environment',{}))
                if v.get('tourapiEnabled'):
                    environment.update(contract['tourapiEnvironment'])
                    if role=='runner':environment.update(contract['tourapiRunnerEnvironment'])
                if trace_enabled:environment['JOBS_TRACE_CONTEXT_WRITE']=str(write).lower()
                if langfuse:environment.update(obs['langfuseEnvironment'])
                require(d['data']==environment,'Exact shared limits/topics and role settings')
        elif kind=='ServiceAccount':require(name in ['ai-api','ai-runner'] and d['automountServiceAccountToken'] is False,'Dedicated tokenless SA')
        elif kind=='ExternalSecret':
            require(name=='ai-runtime','Exact credentials target');s=d['spec'];data=[{'secretKey':k,'remoteRef':{'key':'yeodam/v3/foundation/ai/runtime','property':k,'version':'AWSCURRENT','decodingStrategy':'None'}} for k in contract['secretKeys']+contract['runnerExtraSecrets']+(obs['langfuseSecrets'] if langfuse else [])]
            require(s=={'refreshPolicy':'Periodic','refreshInterval':'1h','secretStoreRef':{'kind':'SecretStore','name':'aws-secretsmanager'},'target':{'name':'ai-runtime','creationPolicy':'Orphan','deletionPolicy':'Retain','template':{'type':'Opaque'}},'data':data},'No production/cross-namespace source')
        elif kind=='Service':require(name=='ai-api' and d['spec']=={'type':'ClusterIP','selector':{'app.kubernetes.io/instance':'ai-api','yeodam.io/stage':'v3'},'ports':[{'name':'http','port':8000,'targetPort':'http'}]},'Private API service only')
        elif kind=='Deployment':
            role=next(k for k,w in contract['workloads'].items() if w['name']==name);w=contract['workloads'][role];s=d['spec'];pod=s['template']['spec'];c=pod['containers'][0]
            annotations=s['template']['metadata']['annotations'];require(annotations.get('yeodam.io/configuration-revision')==v['configurationRevision'] and re.fullmatch('[a-f0-9]{64}',annotations.get('checksum/runtime','')),'Secret-version and Chart configuration must trigger Pod replacement')
            if trace_enabled:
                expected_checksum=hashlib.sha256(((CHART/'files/observability.json').read_text()+str(write).lower()+str(ever).lower()+str(langfuse).lower()).encode()).hexdigest()
                require(annotations.get('checksum/observability')==expected_checksum,'Trace/Langfuse setting changes must replace both Pods')
            else:require('checksum/observability' not in annotations,'No implicit observability feature activation')
            require(role in roles and s['replicas']==1 and s['strategy']=={'type':'Recreate'},'One process copy per role; no surge')
            require(pod['serviceAccountName']==name and not pod['automountServiceAccountToken'] and pod['imagePullSecrets']==[{'name':'ghcr-pull'}],'Exact identity and registry references')
            require(pod['nodeSelector']=={'yeodam.io/role':w['nodeRole'],'yeodam.io/stage':'v3'} and pod['tolerations']==[{'key':'yeodam.io/role','operator':'Equal','value':w['nodeRole'],'effect':'NoSchedule'}],'API/worker NodePool placement')
            require(pod['securityContext']=={'runAsNonRoot':True,'runAsUser':10001,'runAsGroup':10001,'fsGroup':10001,'seccompProfile':{'type':'RuntimeDefault'}},'Nonroot runtime')
            require(len(pod['containers'])==1 and c['image']==v['image'] and c['resources']==w['resources'] and c['command']==w['command'],'Same immutable image and exact minimum role budget')
            require(pod['terminationGracePeriodSeconds']==w['grace'],'Explicit current application shutdown contract')
            for container in pod['initContainers']+[c]:require(container['securityContext']=={'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},'Restricted containers')
            apm=role in apm_roles
            if apm:
                require(pod['initContainers'][-1]==apm_contract.init('python'),'Pinned Python init only')
            require(len(pod['initContainers'])==(2 if apm else 1) and pod['initContainers'][0]['command']==['python','/runtime/prepare-trust.py'] and pod['initContainers'][0]['image']==v['image'],'Public trust bundle first')
            keys=contract['secretKeys']+(contract['runnerExtraSecrets'] if role=='runner' else [])+(obs['langfuseSecrets'] if langfuse else []);require(c['env']==[{'name':k,'valueFrom':{'secretKeyRef':{'name':'ai-runtime','key':k}}} for k in keys]+(apm_contract.python_env(role,v['source']) if apm else []),'Reference-only role secrets')
            volumes={x['name']:x for x in pod['volumes']};require(not any('hostPath' in x or 'persistentVolumeClaim' in x for x in pod['volumes']),'No host mount or added PVC')
            if apm:require(volumes.get('apm')=={'name':'apm','emptyDir':{'sizeLimit':'64Mi'}} and {'name':'apm','mountPath':'/otel','readOnly':True} in c['volumeMounts'],'Read-only bounded SDK mount')
            else:require('apm' not in volumes,'No implicit instrumentation')
            if legacy:require('model-url' not in volumes,'Preserve the exact previously approved runtime')
            else:require(volumes.get('model-url')=={'name':'model-url','secret':{'secretName':'ai-runtime','items':[{'key':'VLM_BASE_URL','path':'base-url'}]}} and {'name':'model-url','mountPath':'/var/run/yeodam-vlm','readOnly':True} in c['volumeMounts'],'Only the VLM URL is projected for ESO confirmation')
            if role=='api':require(c['readinessProbe']['httpGet']=={'path':'/health','port':'http'} and c['livenessProbe']['exec']['command']==['python','/runtime/health.py','api-live'] and 'reference' not in volumes,'Dependency readiness separate from liveness')
            else:require('livenessProbe' not in c and not c.get('ports') and volumes['reference']=={'name':'reference','emptyDir':{'sizeLimit':'3Gi'}} and c['readinessProbe']['exec']['command']==['python','/runtime/health.py','runner-ready'],'Runner has no invented HTTP endpoint; bounded index cache')
def validate(docs,v):
    return _validate(docs,v,CONTRACT,{p.name:p.read_text() for p in (CHART/'files/runtime').iterdir() if p.is_file()},POLICIES)

def validate_snapshot(docs,v,files):
    # Only the exact registered previous approved Chart may use its old runtime
    # contract. Never trust arbitrary embedded contracts or weaken new releases.
    locks=json.loads((CHART/'rollback-compatibility.json').read_text())
    checksums={p:hashlib.sha256(content).hexdigest() for p,content in files.items()}
    record=next((record for record in locks if checksums==record['files']),None)
    if record is not None:
        contract=json.loads(files['files/contract.json'])
        scripts={p[len('files/runtime/'):]:content.decode() for p,content in files.items() if p.startswith('files/runtime/')}
        return _validate(docs,v,contract,scripts,json.loads(files['files/policies.json']),legacy=record.get('legacy',True))
    return validate(docs,v)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path);a=p.parse_args();full=fixture();cases={'disabled':yaml.safe_load((CHART/'values.yaml').read_text()),'access':dict(full,workloads={'api':False,'runner':False}),'api':dict(full,workloads={'api':True,'runner':False}),'full':full}
    for name,v in cases.items():
        source=render(v);docs=platform.load_documents(source);validate(docs,v);platform.schema_check(source)
        if a.output:a.output.mkdir(parents=True,exist_ok=True);(a.output/('ai-'+name+'.yaml')).write_text(source)
    print('AI default0, access, API-only and full runtime contracts passed; synthetic CA only; no image, AWS, cluster or model accessed')
if __name__=='__main__':main()
