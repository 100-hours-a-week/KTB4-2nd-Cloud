#!/usr/bin/env python3
"""Render pinned autoscaling infrastructure and enforce cost/ownership/security boundaries offline."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import yaml
ROOT=Path(__file__).resolve().parents[1]
CHART=ROOT/'deploy/v3/autoscaling'
def require(ok,message):
    if not ok: raise ValueError(message)
def module(name,file):
    s=importlib.util.spec_from_file_location(name,ROOT/file);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
platform=module('platform','scripts/check-v3-platform.py')
def render(enabled=False,api=False,converter=False,reviewed=False):
    values={'enabled':enabled,'apiEnabled':api,'converterEnabled':converter,'metricsServerReady':reviewed,'replicaOwnershipReviewed':reviewed,'converterReview':{'partitions':2 if reviewed else 1,'keyOrderingReviewed':reviewed,'rebalanceAndShutdownVerified':reviewed}}
    text=subprocess.check_output([os.environ['V3_HELM'],'template','yeodam-v3-scaling',str(CHART),'-f','-'],input=yaml.safe_dump(values),text=True,stderr=subprocess.PIPE)
    return [d for d in yaml.safe_load_all(text)if d]
def upstream():
    lock=json.loads((CHART/'upstream.lock.json').read_text());all_docs=[]
    for name,namespace,chart,file in [('keda','keda','keda-2.21.0.tgz','keda.values.yaml'),('metrics-server','kube-system','metrics-server-3.14.0.tgz','metrics-server.values.yaml')]:
        archive=CHART/'upstream'/chart
        require(hashlib.sha256(archive.read_bytes()).hexdigest()==lock['archives'][chart],'Upstream archive checksum mismatch')
        docs=[d for d in yaml.safe_load_all(subprocess.check_output([os.environ['V3_HELM'],'template',name,str(archive),'--namespace',namespace,'-f',str(CHART/file)],text=True))if d]
        deps=[d for d in docs if d['kind']=='Deployment'];require(len(deps)==(3 if name=='keda'else 1),'Bound management replicas/Pod count')
        for dep in deps:
            pod=dep['spec']['template']['spec'];require(dep['spec'].get('replicas',1)==1 and pod['nodeSelector']=={'yeodam.io/role':'worker','yeodam.io/stage':'v3','kubernetes.io/arch':'amd64','kubernetes.io/os':'linux'} and not pod.get('hostNetwork'),'Existing CPU worker only; no GPU/system pressure or host networking')
            for c in pod['containers']:
                require(c['image'] in lock['images'].values(),'Unpinned operator image')
                require(c['resources']['requests']=={'cpu':'25m','memory':'64Mi'} and c['resources']['limits']=={'cpu':'250m','memory':'192Mi'},'Preserve bounded management budget')
                require('--kubelet-insecure-tls' not in c.get('args',[]),'Never bypass kubelet TLS')
        for d in docs:
            if d['kind']=='APIService':require(not d['spec'].get('insecureSkipTLSVerify')and d['spec'].get('caBundle') or name=='keda'and not d['spec'].get('insecureSkipTLSVerify'),'No insecure aggregate API')
        all_docs+=docs
    # Only write public CRD schemas; generated TLS Secret content stays in memory.
    schema=Path(os.environ['V3_SCHEMA_DIR'])
    for d in all_docs:
        if d['kind']=='CustomResourceDefinition'and d['spec']['group']=='keda.sh':
            for version in d['spec']['versions']:
                if version.get('served'):
                    path=schema/(d['spec']['names']['kind'].lower()+'.json');path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(version['schema']['openAPIV3Schema']))
    return all_docs

def validate(docs,api=False,converter=False):
    hpas=[d for d in docs if d['kind']=='HorizontalPodAutoscaler'];require(len(hpas)==(4 if api else 0),'Only reviewed API HPAs')
    for d in hpas:
        s=d['spec'];require(s['minReplicas']==1 and s['maxReplicas']==2,'No zero or uncontrolled expansion')
        require(s['metrics'][0]['containerResource']['target']['type']=='AverageValue'and s['metrics'][0]['containerResource']['target']['averageValue']=='200m','Explicit CPU target, not misleading percentage of tiny request')
        require(s['behavior']['scaleDown']['stabilizationWindowSeconds']==300,'Slow one-at-a-time scale-in')
    scaled=[d for d in docs if d['kind']=='ScaledObject'];require(len(scaled)==int(converter),'Only reviewed converter scaling')
    for d in scaled:
        s=d['spec'];require(s['minReplicaCount']==1 and s['maxReplicaCount']==2 and len(s['triggers'])==4,'Four independently named lane topics and bounded replicas')
        require(s['fallback']=={'failureThreshold':3,'replicas':1,'behavior':'currentReplicasIfHigher'},'Metric failure must not force a healthy second replica down')
        for t in s['triggers']:
            m=t['metadata'];require(m['tls']=='enable'and m['sasl']=='scram_sha512'and m['unsafeSsl']=='false'and m['allowIdleConsumers']=='false','TLS/SCRAM and partition limits required')
    users=[d for d in docs if d['kind']=='KafkaUser']
    for d in users:
        require(all(x['operations']==['Describe']and x['resource']['patternType']=='literal'for x in d['spec']['authorization']['acls']),'Observer must never consume or publish messages')
    platform.schema_check(yaml.safe_dump_all(docs))

def main():
    require(render()==[],'Disabled Chart must emit nothing')
    upstream()
    validate(render(True,True,False,True),api=True)
    validate(render(True,True,True,True),api=True,converter=True)
    print('Pinned operators, bounded API/Kafka scaling and disabled defaults verified; no live changes')
if __name__=='__main__':main()
