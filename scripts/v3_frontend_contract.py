"""Minimum FE runtime contract; no AWS or secret payload access."""
import json,re
from pathlib import Path
CONTRACT=json.loads((Path(__file__).resolve().parents[1]/'deploy/v3/apps/files/frontend-contract.json').read_text())
def require(ok,message):
    if not ok: raise ValueError(message)
def validate(configs,images):
    require(not configs or set(configs)=={'frontend'},'FE uses a separate release')
    for c in configs.values():
        require(re.fullmatch(r'ghcr.io/100-hours-a-week/yeodam-frontend@sha256:[a-f0-9]{64}',images['frontend']),'Immutable separate FE image required')
        require(not any(c[k] for k in ['environment','configMapRefs','secretRefs','secretVolumes','springProperties']),'FE only uses public config and existing registry Secret')
        data=c['configMapData'];require(set(data)==set(CONTRACT['config'])|{'RELEASE'} and all(data.get(k)==v for k,v in CONTRACT['config'].items()),'FE runtime/SSR endpoint mismatch')
        require(re.fullmatch('[a-f0-9]{40}',data['RELEASE']) and re.fullmatch('[a-f0-9]{64}',c['configurationRevision']),'Explicit source and config revision required')
        res=json.loads(json.dumps(CONTRACT['resources']));res['limits']['cpu']=''
        require(c['resources']==res and c['replicas']==1,'Initial FE budget changed')
        require(c['ports']=={'http':3000} and c['service']=={'enabled':True,'port':3000},'FE HTTP3000 Service required')
        require(c['scratchSizeLimit']=='64Mi' and c['rollout']=={'maxSurge':0,'maxUnavailable':1} and c['terminationGracePeriodSeconds']==60 and c['preStopDelaySeconds']==5,'FE scratch/shutdown budget changed')
        for key in ['startup','liveness','readiness']:
            require(c['probes'][key]['httpGet']=={'path':'/login','port':'http','scheme':'HTTP'},'FE Probe must use actual public route')
