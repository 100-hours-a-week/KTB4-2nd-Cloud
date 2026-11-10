#!/usr/bin/env python3
"""AI-only host log collection contract; no AWS or application mutations."""
import importlib.util,json,os,subprocess
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]
CHART=ROOT/'deploy/v3/ai-logs'
def require(ok,message):
    if not ok:raise ValueError(message)
def render(enabled):
    return list(filter(None,yaml.safe_load_all(subprocess.check_output([os.environ.get('V3_HELM','helm'),'template','ai-logs',str(CHART),'--kube-version','1.36.0','--set','enabled='+str(enabled).lower()],text=True))))
def validate(docs):
    require(len(docs)==5 and {x['kind'] for x in docs}=={'Namespace','ServiceAccount','ConfigMap','DaemonSet','NetworkPolicy'},'Only collector resources; no app, Secret or RBAC mutation')
    require(all(x['metadata'].get('namespace')=='observability' for x in docs if x['kind']!='Namespace'),'Dedicated namespace')
    pod=next(x for x in docs if x['kind']=='DaemonSet')['spec']['template']['spec'];c=pod['containers'][0]
    require(c['image']==json.loads((CHART/'image.lock.json').read_text())['image'] and '@sha256:' in c['image'],'Pinned official collector image')
    require(pod['serviceAccountName']=='ai-log-collector' and pod['automountServiceAccountToken'] is False,'No Kubernetes API token')
    require(pod['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms']==[{'matchExpressions':[{'key':'karpenter.sh/nodepool','operator':'In','values':['api','worker']}]}],'Only app nodes; no broker/system/GPU placement')
    require(not pod.get('hostNetwork') and not pod.get('hostPID') and c['securityContext']=={'runAsUser':0,'runAsGroup':0,'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},'Restricted collector, no privileged container')
    require(c['resources']=={'requests':{'cpu':'10m','memory':'32Mi','ephemeral-storage':'32Mi'},'limits':{'cpu':'200m','memory':'128Mi','ephemeral-storage':'256Mi'}},'Bounded daemon overhead; no Node creation')
    mounts={v['name']:v for v in c['volumeMounts']};volumes={v['name']:v for v in pod['volumes']}
    require(mounts['logs']['readOnly'] and volumes['logs']['hostPath']=={'path':'/var/log','type':'Directory'} and volumes['buffer']['hostPath']['path']=='/var/lib/yeodam/ai-log-collector','Read-only existing logs and dedicated bounded buffer')
    cm=next(x for x in docs if x['kind']=='ConfigMap')['data']
    for name in ['fluent-bit.conf','parsers.conf','sanitize.lua']:require(cm[name]==(CHART/'files'/name).read_text(),'Reviewed pipeline sources only')
    cfg=cm['fluent-bit.conf']
    require(cfg.count('auto_create_group false')==2 and cfg.count('storage.total_limit_size 64M')==2 and cfg.count('Read_From_Head Off')==2,'No group creation/backfill; bounded retry buffers')
    require('Path /var/log/containers/ai-api-*_ai_ai-*.log' in cfg and 'Path /var/log/containers/ai-runner-*_ai_ai-*.log' in cfg,'Exact AI container source paths')
    policy=next(x for x in docs if x['kind']=='NetworkPolicy')['spec']
    require(policy['ingress']==[] and set(policy['policyTypes'])=={'Ingress','Egress'},'No public receiver')
    return True
if __name__=='__main__':
    require(render(False)==[],'Disabled must render nothing')
    docs=render(True);validate(docs)
    spec=importlib.util.spec_from_file_location('platform',ROOT/'scripts/check-v3-platform.py');p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
    p.schema_check(yaml.safe_dump_all(docs))
    print('AI-only collector, bounded resources and no Kubernetes API privileges verified')
