"""Reviewed replica ownership and read-only live prerequisites; never scale a workload."""
from copy import deepcopy
API = {'frontend':'frontend','auth-api':'auth','trip-api':'trip','photo-api':'photo'}
SCALABLE = {**API,'photo-converter':'photo'}
LANES = ('initial-light','initial-heic','additional-light','additional-heic')
def require(ok,message):
    if not ok: raise ValueError(message)
def prepare_candidate(candidate,converter=False):
    result = deepcopy(candidate)
    require(result.get('schemaVersion') == 2 and result.get('state')=='prepared' and result.get('target')=='yeodam-v3-foundation','Use a complete reviewed candidate')
    result['values']['frontend']['autoscaledWorkloads']=['frontend']
    result['values']['backend']['autoscaledWorkloads']=['auth-api','trip-api','photo-api']+(['photo-converter'] if converter else [])
    return result

def validate_prerequisites(items,converter=False):
    """Use public GET evidence only. All claims must be explicit, no empty/unknown => pass."""
    by={(d['kind'],d.get('metadata',{}).get('namespace'),d['metadata']['name']):d for d in items}
    api=by.get(('APIService',None,'v1beta1.metrics.k8s.io'),{})
    require(any(c.get('type')=='Available'and c.get('status')=='True'for c in api.get('status',{}).get('conditions',[])),'metrics.k8s.io must be Available')
    targets=dict(API)
    if converter: targets['photo-converter']='photo'
    for name,ns in targets.items():
        d=by.get(('Deployment',ns,name),{})
        require(d.get('status',{}).get('availableReplicas',0)>=1,'Target must already be available: '+name)
        for other in items:
            if other.get('metadata',{}).get('namespace')!=ns:continue
            if other['kind']=='HorizontalPodAutoscaler':
                expected='keda-hpa-photo-converter'if name=='photo-converter'else name
                require(other['metadata']['name']==expected or other.get('spec',{}).get('scaleTargetRef',{}).get('name')!=name,'Competing HPA rejected')
            if other['kind']=='ScaledObject':
                require(name=='photo-converter'and other['metadata']['name']==name or other.get('spec',{}).get('scaleTargetRef',{}).get('name')!=name,'Competing ScaledObject rejected')
    if converter:
        for lane in LANES:
            d=by.get(('KafkaTopic','kafka','prod.yeodam.image.'+lane),{})
            require(d.get('spec',{}).get('partitions')==2 and any(c.get('type')=='Ready'and c.get('status')=='True'for c in d.get('status',{}).get('conditions',[])),'All four image topics need two Ready partitions')
        dep=by[('Deployment','photo','photo-converter')]
        require(dep['spec']['template']['spec']['terminationGracePeriodSeconds']>=180,'Preserve shutdown/drain grace')
        require(not any(x['kind']=='HorizontalPodAutoscaler'and x.get('metadata',{}).get('namespace')=='photo'and x.get('spec',{}).get('scaleTargetRef',{}).get('name')=='photo-converter' and x.get('metadata',{}).get('name')!='keda-hpa-photo-converter' for x in items),'KEDA is the only converter HPA owner')
    return {'targets':sorted(targets),'minReplicas':1,'maxReplicas':2,'liveChanges':False}
