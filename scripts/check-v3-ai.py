#!/usr/bin/env python3
"""Offline minimum AI runtime/connection contracts. No cluster, image pull or model call."""
import argparse,copy,importlib.util,json,os,re,subprocess,tempfile
from collections import Counter
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1];CHART=ROOT/'deploy/v3/ai-apps';CONTRACT=json.loads((CHART/'files/contract.json').read_text());POLICIES=json.loads((CHART/'files/policies.json').read_text())
spec=importlib.util.spec_from_file_location('platform',ROOT/'scripts/check-v3-platform.py');platform=importlib.util.module_from_spec(spec);spec.loader.exec_module(platform);require=platform.require

def fixture():
    with tempfile.TemporaryDirectory(prefix='ai-ca-fixture-') as d:
        ca=Path(d)/'ca.crt';key=Path(d)/'ca.key';subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=synthetic-ci-only','-keyout',str(key),'-out',str(ca)],check=True,capture_output=True);public=ca.read_text()
    return {'accessEnabled':True,'workloads':{'api':True,'runner':True},'image':'ghcr.io/100-hours-a-week/yeodam-ai-app@sha256:'+'a'*64,'source':'b'*40,'configurationRevision':'c'*64,'review':{'sourceCompatible':True,'imageReviewed':True,'connectionsReviewed':True},'trust':{k:public for k in ['redis','qdrant','kafka']},'privateModelPeers':[]}
def render(values=None):
    args=[os.environ['V3_HELM'],'template','yeodam-v3-ai-apps',str(CHART),'-n','ai','--kube-version','1.36.0']
    return subprocess.check_output(args+(['-f','-'] if values else []),input=yaml.safe_dump(values) if values else None,text=True,stderr=subprocess.PIPE)
def validate(docs,v):
    require(CONTRACT['workloads']['runner'].get('environment',{}).get('PYTHONPATH')=='/app','File-based runner must resolve the /app package')
    enabled=v['accessEnabled'];roles=[r for r,x in v['workloads'].items() if x];expected={}
    if enabled:
        expected={'ConfigMap':4,'ServiceAccount':2,'ExternalSecret':1,'NetworkPolicy':7+bool(v['privateModelPeers'])}
        if roles:expected['Deployment']=len(roles)
        if v['workloads']['api']:expected['Service']=1
    require(dict(Counter(d['kind'] for d in docs))==expected,'Exact inventory; no Namespace/PVC/Secret/Ingress')
    if not enabled:return
    require(not roles or all(v['review'].values()),'All source/image/model reviews required')
    identities=[(d['kind'],d['metadata']['namespace'],d['metadata']['name']) for d in docs];require(len(identities)==len(set(identities)),'Duplicate resources')
    expectedPolicies={(p['namespace'],p['name']):p['spec'] for p in POLICIES}
    if v['privateModelPeers']:expectedPolicies[('ai','allow-ai-reviewed-private-models')]={'podSelector':{'matchLabels':{'yeodam.io/ai-runtime':'true','yeodam.io/stage':'v3'}},'policyTypes':['Egress'],'egress':[{'to':[{'ipBlock':{'cidr':p['cidr']}}],'ports':[{'protocol':'TCP','port':p['port']}]} for p in v['privateModelPeers']]}
    for d in docs:
        n=d['metadata']['namespace'];name=d['metadata']['name'];kind=d['kind']
        if kind=='NetworkPolicy':require((n,name) in expectedPolicies and d['spec']==expectedPolicies[(n,name)],'Exact cross-namespace peers and ports');continue
        require(n=='ai','Only policies may cross the AI namespace')
        if kind=='ConfigMap':
            if name=='ai-trust':require(d['data']=={k+'.crt':v['trust'][k] for k in ['redis','qdrant','kafka']},'Public CA references only')
            elif name=='ai-runtime-scripts':require(d['data']=={p.name:p.read_text() for p in (CHART/'files/runtime').iterdir() if p.is_file()},'Runtime scripts pinned')
            else:
                role='api' if name=='ai-api-runtime' else 'runner';require(d['data']==dict(CONTRACT['commonEnvironment'],RELEASE=v['source'],**CONTRACT['workloads'][role].get('environment',{})),'Exact shared limits/topics and role settings')
        elif kind=='ServiceAccount':require(name in ['ai-api','ai-runner'] and d['automountServiceAccountToken'] is False,'Dedicated tokenless SA')
        elif kind=='ExternalSecret':
            require(name=='ai-runtime','Exact credentials target');s=d['spec'];data=[{'secretKey':k,'remoteRef':{'key':'yeodam/v3/foundation/ai/runtime','property':k,'version':'AWSCURRENT','decodingStrategy':'None'}} for k in CONTRACT['secretKeys']+CONTRACT['runnerExtraSecrets']]
            require(s=={'refreshPolicy':'Periodic','refreshInterval':'1h','secretStoreRef':{'kind':'SecretStore','name':'aws-secretsmanager'},'target':{'name':'ai-runtime','creationPolicy':'Orphan','deletionPolicy':'Retain','template':{'type':'Opaque'}},'data':data},'No production/cross-namespace source')
        elif kind=='Service':require(name=='ai-api' and d['spec']=={'type':'ClusterIP','selector':{'app.kubernetes.io/instance':'ai-api','yeodam.io/stage':'v3'},'ports':[{'name':'http','port':8000,'targetPort':'http'}]},'Private API service only')
        elif kind=='Deployment':
            role=next(k for k,w in CONTRACT['workloads'].items() if w['name']==name);w=CONTRACT['workloads'][role];s=d['spec'];pod=s['template']['spec'];c=pod['containers'][0]
            annotations=s['template']['metadata']['annotations'];require(annotations.get('yeodam.io/configuration-revision')==v['configurationRevision'] and re.fullmatch('[a-f0-9]{64}',annotations.get('checksum/runtime','')),'Secret-version and Chart configuration must trigger Pod replacement')
            require(role in roles and s['replicas']==1 and s['strategy']=={'type':'Recreate'},'One process copy per role; no surge')
            require(pod['serviceAccountName']==name and not pod['automountServiceAccountToken'] and pod['imagePullSecrets']==[{'name':'ghcr-pull'}],'Exact identity and registry references')
            require(pod['nodeSelector']=={'yeodam.io/role':w['nodeRole'],'yeodam.io/stage':'v3'} and pod['tolerations']==[{'key':'yeodam.io/role','operator':'Equal','value':w['nodeRole'],'effect':'NoSchedule'}],'API/worker NodePool placement')
            require(pod['securityContext']=={'runAsNonRoot':True,'runAsUser':10001,'runAsGroup':10001,'fsGroup':10001,'seccompProfile':{'type':'RuntimeDefault'}},'Nonroot runtime')
            require(len(pod['containers'])==1 and c['image']==v['image'] and c['resources']==w['resources'] and c['command']==w['command'],'Same immutable image and exact minimum role budget')
            require(pod['terminationGracePeriodSeconds']==w['grace'],'Explicit current application shutdown contract')
            for container in pod['initContainers']+[c]:require(container['securityContext']=={'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},'Restricted containers')
            require(len(pod['initContainers'])==1 and pod['initContainers'][0]['command']==['python','/runtime/prepare-trust.py'] and pod['initContainers'][0]['image']==v['image'],'Public trust bundle first')
            keys=CONTRACT['secretKeys']+(CONTRACT['runnerExtraSecrets'] if role=='runner' else []);require(c['env']==[{'name':k,'valueFrom':{'secretKeyRef':{'name':'ai-runtime','key':k}}} for k in keys],'Reference-only role secrets')
            volumes={x['name']:x for x in pod['volumes']};require(not any('hostPath' in x or 'persistentVolumeClaim' in x for x in pod['volumes']),'No host mount or added PVC')
            require(volumes.get('model-url')=={'name':'model-url','secret':{'secretName':'ai-runtime','items':[{'key':'VLM_BASE_URL','path':'base-url'}]}} and {'name':'model-url','mountPath':'/var/run/yeodam-vlm','readOnly':True} in c['volumeMounts'],'Only the VLM URL is projected for ESO confirmation')
            if role=='api':require(c['readinessProbe']['httpGet']=={'path':'/health','port':'http'} and c['livenessProbe']['exec']['command']==['python','/runtime/health.py','api-live'] and 'reference' not in volumes,'Dependency readiness separate from liveness')
            else:require('livenessProbe' not in c and not c.get('ports') and volumes['reference']=={'name':'reference','emptyDir':{'sizeLimit':'3Gi'}} and c['readinessProbe']['exec']['command']==['python','/runtime/health.py','runner-ready'],'Runner has no invented HTTP endpoint; bounded index cache')
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path);a=p.parse_args();full=fixture();cases={'disabled':yaml.safe_load((CHART/'values.yaml').read_text()),'access':dict(full,workloads={'api':False,'runner':False}),'api':dict(full,workloads={'api':True,'runner':False}),'full':full}
    for name,v in cases.items():
        source=render(v);docs=platform.load_documents(source);validate(docs,v);platform.schema_check(source)
        if a.output:a.output.mkdir(parents=True,exist_ok=True);(a.output/('ai-'+name+'.yaml')).write_text(source)
    print('AI default0, access, API-only and full runtime contracts passed; synthetic CA only; no image, AWS, cluster or model accessed')
if __name__=='__main__':main()
