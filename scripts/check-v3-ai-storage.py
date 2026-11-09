#!/usr/bin/env python3
"""Validate isolated AI storage manifests, minimum budgets and persistence. Offline only."""
import argparse,importlib.util,json,os,subprocess
from pathlib import Path
from collections import Counter
import yaml
ROOT=Path(__file__).resolve().parents[1];CHART=ROOT/'deploy/v3/ai-storage'
spec=importlib.util.spec_from_file_location('platform',ROOT/'scripts/check-v3-platform.py');platform=importlib.util.module_from_spec(spec);spec.loader.exec_module(platform)
require=platform.require
CONTRACT=json.loads((CHART/'files/contract.json').read_text());POLICIES=json.loads((CHART/'files/policies.json').read_text());IMAGES=yaml.safe_load((CHART/'values.yaml').read_text())['images']
def render(enabled=False):
    return subprocess.check_output([os.environ['V3_HELM'],'template','yeodam-v3-ai-storage',str(CHART),'--kube-version','1.36.0','--set','enabled='+str(enabled).lower()],text=True)
def validate(docs,enabled=False):
    expected={'Namespace':2,'NetworkPolicy':7}
    if enabled:expected.update(ServiceAccount=2,ConfigMap=2,Service=4,StatefulSet=2)
    require(dict(Counter(d['kind'] for d in docs))==expected,'Exact inventory; no embedded Secrets or extra workloads')
    keys=[(d['kind'],d['metadata'].get('namespace',''),d['metadata']['name']) for d in docs];require(len(keys)==len(set(keys)),'Duplicate resource')
    for d in docs:
        if d['kind']=='Namespace':
            require(d['metadata']['name'] in CONTRACT and d['metadata']['annotations']=={'helm.sh/resource-policy':'keep'} and d['metadata']['labels']['pod-security.kubernetes.io/enforce']=='restricted','Retained restricted storage namespace only')
        elif d['kind']=='NetworkPolicy':
            p=next((p for p in POLICIES if p['namespace']==d['metadata']['namespace'] and p['name']==d['metadata']['name']),None)
            require(p and d['spec']==p['spec'],'Exact AI-only network peers; no public or BE access')
        else:require(d['metadata']['namespace'] in CONTRACT,'Storage namespace boundary')
    for d in docs:
        if d['kind']=='ConfigMap':
            n=d['metadata']['namespace'];folder=CHART/'files'/('redis' if n=='ai-redis' else 'qdrant');require(d['data']=={p.name:p.read_text() for p in folder.iterdir()},'Fixed runtime configuration')
        if d['kind']=='Service':
            n=d['metadata']['namespace'];s=d['spec'];require(s['ports']==[{'name':'tls','port':CONTRACT[n]['port'],'targetPort':'tls'}] and s['selector']=={'app.kubernetes.io/instance':n,'yeodam.io/stage':'v3'} and s.get('type','ClusterIP')=='ClusterIP' and not s.get('externalIPs'),'Private selected service only')
        if d['kind']!='StatefulSet':continue
        n=d['metadata']['name'];s=d['spec'];c=CONTRACT[n];pod=s['template']['spec'];container=pod['containers'][0]
        require(d['metadata']['namespace']==n and s['replicas']==1 and s['updateStrategy']=={'type':'OnDelete'},'One member; explicit replacement only')
        require(s['persistentVolumeClaimRetentionPolicy']=={'whenDeleted':'Retain','whenScaled':'Retain'},'Preserve PVC on scale/delete')
        require(len(s['volumeClaimTemplates'])==1 and s['volumeClaimTemplates'][0]['spec']=={'accessModes':['ReadWriteOncePod'],'storageClassName':'yeodam-v3-gp3-retain','resources':{'requests':{'storage':c['storage']}}},'Retained single writer gp3 budget')
        require(pod['nodeSelector']=={'yeodam.io/role':'data','yeodam.io/stage':'v3'} and pod['tolerations']==[{'key':'yeodam.io/role','operator':'Equal','value':'data','effect':'NoSchedule'}],'Existing data Nodes only')
        require(len(pod['containers'])==1 and not pod.get('initContainers') and not pod['automountServiceAccountToken'],'No extra process or Kubernetes token')
        require(pod['securityContext']=={'runAsNonRoot':True,'runAsUser':c['uid'],'runAsGroup':c['uid'],'fsGroup':c['uid'],'fsGroupChangePolicy':'OnRootMismatch','seccompProfile':{'type':'RuntimeDefault'}},'Nonroot storage ownership')
        require(container['securityContext']=={'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},'Restricted container')
        require(container['image']==IMAGES[c['imageKey']] and container['resources']==c['resources'],'Pinned image and minimum resources')
        require(pod['terminationGracePeriodSeconds']==120,'Storage shutdown grace')
        require(next(v for v in pod['volumes'] if v['name']=='credentials')['secret']=={'secretName':c['secret'],'defaultMode':288},'Exact namespaced credentials')
        require(any(m['name']=='data' and m['mountPath']=='/data' for m in container['volumeMounts']),'Data on retained volume')
        for name in ['startupProbe','readinessProbe','livenessProbe']:
            p=container[name];require(p['timeoutSeconds']==5 and p['periodSeconds']==10,'Bounded local health probes')
            if n=='qdrant':require(p['httpGet']=={'path':'/livez' if name=='livenessProbe' else '/readyz','port':'tls','scheme':'HTTPS'},'HTTPS local health only; auth verified separately')
            else:require(p['exec']=={'command':['/bin/sh','/runtime/health.sh']},'Redis authenticated health')
        if n=='qdrant':require(container['env']==[{'name':'QDRANT__SERVICE__API_KEY','valueFrom':{'secretKeyRef':{'name':'qdrant-bootstrap','key':'api-key'}}}],'Qdrant API key required')
    redis=(CHART/'files/redis/redis.conf').read_text();require(all(x in redis.splitlines() for x in ['port 0','tls-port 6379','appendonly yes','appendfsync everysec','maxmemory 64mb','maxmemory-policy noeviction']),'TLS-only durable non-evicting queue')
    start=(CHART/'files/redis/start.sh').read_text()
    require("user app on #%s ~* &* +@connection +@read +@write +@transaction +@scripting +@pubsub -@admin -@dangerous +info +time\\n" in start,'AI queue requires TIME while administrative commands remain denied')
    q=yaml.safe_load((CHART/'files/qdrant/qdrant.yaml').read_text());require(q['service']['enable_tls'] and q['service']['grpc_port'] is None and q['cluster']=={'enabled':False} and q['telemetry_disabled'],'TLS REST only standalone')
    require(q['storage']['storage_path']=='/data/storage' and q['storage']['snapshots_path']=='/data/snapshots','Keep vectors and snapshots on PVC')
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path);a=p.parse_args()
    for enabled in [False,True]:
        source=render(enabled);validate(platform.load_documents(source),enabled);platform.schema_check(source)
        if a.output:a.output.mkdir(parents=True,exist_ok=True);(a.output/('ai-storage-enabled.yaml' if enabled else 'ai-storage-disabled.yaml')).write_text(source)
    print('AI storage disabled/enabled render, schema and isolation contracts passed; no AWS or cluster accessed')
if __name__=='__main__':main()
