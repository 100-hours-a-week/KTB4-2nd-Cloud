#!/usr/bin/env python3
"""Bound APM to two private observer Pods. No credentials, AWS or cluster actions."""
import importlib.util,json,os,subprocess
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1];CHART=ROOT/'deploy/v3/apm'
def require(ok,message):
    if not ok:raise ValueError(message)
def render(enabled=True):
    source=subprocess.check_output([os.environ.get('V3_HELM','helm'),'template','v3-apm',str(CHART),'--set','enabled='+str(enabled).lower()],text=True)
    return [x for x in yaml.safe_load_all(source) if x]
def validate(docs):
    kinds={'ServiceAccount','ConfigMap','Service','Deployment','PersistentVolumeClaim','NetworkPolicy'}
    require(all(x['kind'] in kinds for x in docs),'No AWS, Secret, public endpoint, operator or cluster authority')
    deployments=[x for x in docs if x['kind']=='Deployment'];require(len(deployments)==2,'Two new Pods only')
    images=json.loads((CHART/'images.lock.json').read_text())
    for d in deployments:
        pod=d['spec']['template']['spec'];c=pod['containers'][0];name=c['name']
        require(d['spec']['replicas']==1 and pod['nodeSelector']=={'yeodam.io/role':'system','kubernetes.io/os':'linux','kubernetes.io/arch':'amd64'},'Existing system nodes only')
        require(pod['automountServiceAccountToken'] is False and not pod.get('hostNetwork') and not pod.get('hostPID'),'No host/API access')
        require(pod['securityContext']['runAsNonRoot'] and c['securityContext']=={'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},'Restricted non-root runtime')
        require(c['image']==images[name],'Pinned official image')
        budget=('25m','64Mi','200m','128Mi') if name=='alloy' else ('50m','128Mi','500m','384Mi')
        q=c['resources'];require((q['requests']['cpu'],q['requests']['memory'],q['limits']['cpu'],q['limits']['memory'])==budget,'Fixed minimum budget')
    pvc=[x for x in docs if x['kind']=='PersistentVolumeClaim'];require(len(pvc)==1 and pvc[0]['spec']['resources']['requests']['storage']=='3Gi','Single bounded disk')
    for d in docs:
        if d['kind']=='Service':require(d['spec']['type']=='ClusterIP','No public APM ingress')
    cfg=next(x for x in docs if x['kind']=='ConfigMap')['data']
    require(cfg['config.alloy']==(CHART/'files/config.alloy').read_text(),'Reviewed sanitization pipeline only')
    tempo=yaml.safe_load(cfg['tempo.yaml'])
    require(tempo['overrides']['defaults']['compaction']['block_retention']=='24h' and tempo['overrides']['defaults']['ingestion']['rate_limit_bytes']==16384,'Short retention and strict ingestion budget')
    budget=json.loads((CHART/'budget.json').read_text())
    ingestion=tempo['overrides']['defaults']['ingestion']
    require(ingestion['burst_size_bytes']==budget['tempoBurstBytes'] and ingestion['max_traces_per_user']==budget['maxActiveTraces'], 'Bounded burst/active trace budget')
    require(tempo['overrides']['defaults']['global']['max_bytes_per_trace']==budget['maxTraceBytes'], 'Reviewed finite trace size')
    require(budget['workflows']*budget['maxWireBytesPerWorkflow']<=budget['alloyQueueBytes'] and budget['maxWireBytesPerWorkflow']<budget['maxTraceBytes'], 'Envelope must fit queue and per-trace cap')
    require('sizer = "bytes"' in cfg['config.alloy'] and 'queue_size = '+str(budget['alloyQueueBytes']) in cfg['config.alloy'], 'Queue must be byte bounded')
    require(tempo['storage']['trace']['backend']=='local' and tempo['usage_report']['reporting_enabled'] is False,'No remote storage/account dependency')
    for chart in ['apps','ai-apps']:
        lock=json.loads((ROOT/f'deploy/v3/{chart}/files/apm.json').read_text())
        require(lock['javaImage']==images['java'] and lock['pythonImage']==images['python'] and lock['samplerArg']=='0.01','One reviewed bundle and sampling contract')
    return True
if __name__=='__main__':
    require(render(False)==[],'Default creates nothing');docs=render();validate(docs)
    spec=importlib.util.spec_from_file_location('platform',ROOT/'scripts/check-v3-platform.py');p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p);p.schema_check(yaml.safe_dump_all(docs))
    print('Two private APM Pods, bounded buffers/retention and pinned per-role injection verified')
