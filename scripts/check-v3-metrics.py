#!/usr/bin/env python3
"""Render and check bounded metrics resources; never access AWS or Secrets."""
import importlib.util
import json
import os
import subprocess
from pathlib import Path
import yaml
ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'deploy/v3/metrics'

def require(ok, message):
    if not ok:
        raise ValueError(message)

def render(enabled=True, ai=True, apm=False):
    source = subprocess.check_output([os.environ.get('V3_HELM','helm'),'template','v3-metrics',str(CHART),'--kube-version','1.36.0','--set','enabled='+str(enabled).lower(),'--set','aiMetricsEnabled='+str(ai).lower(),'--set','apmEnabled='+str(apm).lower()],text=True)
    return [x for x in yaml.safe_load_all(source) if x]

def validate(docs):
    allowed={'Deployment','Service','ServiceAccount','ConfigMap','PersistentVolumeClaim','Role','RoleBinding','ClusterRole','ClusterRoleBinding','NetworkPolicy'}
    require(all(x['kind'] in allowed for x in docs),'No app, AWS, external endpoint or Secret creation')
    deps=[x for x in docs if x['kind']=='Deployment']
    require(len(deps)==4,'Exactly four observation Pods')
    images=json.loads((CHART/'images.lock.json').read_text())
    for d in deps:
        require(d['metadata']['namespace']=='observability','Observation namespace only')
        pod=d['spec']['template']['spec'];c=pod['containers'][0]
        require(d['spec']['replicas']==1 and pod['nodeSelector']=={'yeodam.io/role':'system','kubernetes.io/os':'linux','kubernetes.io/arch':'amd64'},'Existing system nodes only; no replicas increase')
        budgets={'prometheus':('100m','256Mi','500m','512Mi'),'grafana':('50m','128Mi','250m','256Mi'),'kube-state-metrics':('10m','32Mi','100m','64Mi'),'cdc-status':('10m','16Mi','100m','64Mi')}
        budget=budgets[c['name']];r=c['resources']
        require((r['requests']['cpu'],r['requests']['memory'],r['limits']['cpu'],r['limits']['memory'])==budget,'Fixed minimum resource budget')
        require(c['image']==images[c['name']] and '@sha256:' in c['image'],'Pinned image')
        require(pod['securityContext']['runAsNonRoot'] and c['securityContext']=={'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},'Non-root and restricted')
        require(not pod.get('hostNetwork') and not pod.get('hostPID') and not any('hostPath' in v for v in pod.get('volumes',[])),'No host access')
    for obj in docs:
        if obj['kind'] in ['Role','ClusterRole']:
            for rule in obj['rules']:
                require(set(rule['verbs'])<={'get','list','watch'} and not set(rule['resources']) & {'secrets','nodes/proxy','pods/exec'},'No secret access, execution, arbitrary proxy or writes')
        if obj['kind']=='Service':require(obj['spec']['type']=='ClusterIP','No public listener')
    pvc=[x for x in docs if x['kind']=='PersistentVolumeClaim']
    require(len(pvc)==1 and pvc[0]['spec']['resources']['requests']['storage']=='5Gi','One bounded PVC')
    prom=next(d for d in deps if d['metadata']['name']=='v3-prometheus')['spec']['template']['spec']['containers'][0]
    require('--storage.tsdb.retention.time=3d' in prom['args'] and '--storage.tsdb.retention.size=4GB' in prom['args'],'Bound time and bytes')
    cfg=yaml.safe_load(next(x for x in docs if x['kind']=='ConfigMap' and x['metadata']['name']=='v3-metrics-config')['data']['prometheus.yaml'])
    require(all(x.get('sample_limit',0)>0 for x in cfg['scrape_configs']),'Bound every scrape')
    own=next(x for x in cfg['scrape_configs'] if x['job_name']=='prometheus')
    own_names={'prometheus_tsdb_head_series','prometheus_tsdb_head_samples_appended_total','prometheus_tsdb_storage_blocks_bytes','prometheus_tsdb_wal_storage_size_bytes','prometheus_tsdb_retention_limit_bytes','prometheus_target_scrapes_exceeded_sample_limit_total','prometheus_config_last_reload_successful','prometheus_config_last_reload_success_timestamp_seconds','process_resident_memory_bytes','process_cpu_seconds_total','go_memstats_heap_inuse_bytes'}
    rules=own.get('metric_relabel_configs',[])
    require(own['sample_limit']==1000 and len(rules)==1 and rules[0].get('source_labels')==['__name__'] and rules[0].get('action')=='keep' and set(rules[0].get('regex','').split('|'))==own_names,'Self metrics must stay within the reviewed scalar allowlist and original sample budget')
    grafana=next(d for d in deps if d['metadata']['name']=='v3-grafana')['spec']['template']['spec']['containers'][0]
    env={x['name']:x.get('value')for x in grafana['env']}
    require(env['GF_AUTH_ANONYMOUS_ORG_ROLE']=='Viewer' and env['GF_AUTH_BASIC_ENABLED']=='false' and env['GF_SECURITY_DISABLE_INITIAL_ADMIN_CREATION']=='true','Viewer only; no administrator or login expansion')
    apm=any(x['job_name']=='apm-tempo'for x in cfg['scrape_configs'])
    require(env.get('GF_USERS_VIEWERS_CAN_EDIT')==('true'if apm else None),'APM Explore UI only; persisted dashboard writes remain forbidden')
    ai=[x for x in cfg['scrape_configs'] if x['job_name']=='ai']
    if ai:require(ai[0]['authorization']=={'type':'Bearer','credentials_file':'/etc/prometheus/ai-auth/API_KEY'},'AI auth only by pre-supplied file')
    # Kubernetes network policy cannot enforce HTTP methods; exporter code is GET-only.
    return True

if __name__=='__main__':
    require(render(False)==[],'Default disabled')
    docs=render();validate(docs)
    spec=importlib.util.spec_from_file_location('platform',ROOT/'scripts/check-v3-platform.py');p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
    p.schema_check(yaml.safe_dump_all(docs))
    print('Bounded metrics Chart, private access and read-only Kubernetes authority verified')
