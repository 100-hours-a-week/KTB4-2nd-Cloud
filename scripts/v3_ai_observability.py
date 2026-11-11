"""AI reader-first and sticky rollback floor. Public configuration only."""
from copy import deepcopy
import re


def require(ok, message):
    if not ok:raise ValueError(message)


def transition(before, after):
    old=(before or {}).get('values',{}).get('ai',{})
    new=after['values']['ai']
    if old.get('traceContextWriteEverEnabled'):
        require(new.get('traceContextWriteEverEnabled') and new.get('traceContextEnabled'),
                'Existing Redis carriers require the compatible reader; old strict rollback blocked')
    if new.get('traceContextWriteEverEnabled') and not old.get('traceContextWriteEverEnabled'):
        require(new.get('traceContextWriteEnabled'),'Set compatibility floor only with first write activation')
    if new.get('traceContextWriteEnabled') and not old.get('traceContextWriteEnabled'):
        require(old.get('traceContextEnabled') and not old.get('traceContextWriteEnabled'),
                'Approve reader-only deployment before enabling carrier writes')
        require(all(old.get(k)==new.get(k)for k in ['image','source']),
                'Do not change the reviewed reader image during write activation')


def readers(deployments, pods, candidate):
    """Review only API/runner Pods. No Secret values or environment values exported."""
    v=candidate['values']['ai'];image=candidate['components']['ai-app']['image']
    require(v.get('traceContextEnabled') and not v.get('traceContextWriteEnabled'),'Reader-only approved snapshot required')
    names={'ai-api','ai-runner'};matched=[]
    for name in sorted(names):
        found=[d for d in deployments if d['metadata']['namespace']=='ai'and d['metadata']['name']==name]
        require(len(found)==1,'Exactly two AI readers required');d=found[0];status=d.get('status',{})
        require(d['spec']['replicas']==1 and status.get('observedGeneration',0)>=d['metadata']['generation']
                and status.get('updatedReplicas')==1 and status.get('readyReplicas')==1 and status.get('replicas')==1,
                'Reader rollout must finish without old replicas')
        current=[p for p in pods if p['metadata']['namespace']=='ai'and p['metadata'].get('labels',{}).get('app.kubernetes.io/instance')==name]
        require(len(current)==1 and not current[0]['metadata'].get('deletionTimestamp'),'Old/terminating reader Pod still present')
        p=current[0];cs=p['spec']['containers'];require(len(cs)==1 and cs[0]['image']==image,'Exact reviewed AI image required')
        ready=any(c['type']=='Ready'and c['status']=='True'for c in p.get('status',{}).get('conditions',[]))
        require(ready and p['status']['phase']=='Running','Reader Pod not Ready')
        require(d['spec']['template']['spec']['containers'][0]['image']==image,'Deployment reader image differs')
        require(cs[0].get('envFrom')==[{'configMapRef':{'name':name+'-runtime'}}],'Unexpected reader environment source')
        require(not any(e['name']=='JOBS_TRACE_CONTEXT_WRITE'for e in cs[0].get('env',[])),'Pod may not override the reviewed false ConfigMap')
        # Effective ConfigMap value is separately checked by the command, not guessed from pod envFrom.
        matched.append({'name':name,'podUid':p['metadata']['uid'],'image':image})
    return matched


def prepare(source, phase):
    result=deepcopy(source);v=result['values']['ai']
    require(v['workloads']=={'api':True,'runner':True} and set(v.get('apmWorkloads',[]))=={'api','runner'},'Preserve both APM-enabled roles')
    v['traceContextEnabled']=True;v['langfuseEnabled']=True
    v['traceContextWriteEnabled']=phase=='writer'
    v['traceContextWriteEverEnabled']=bool(v.get('traceContextWriteEverEnabled')) or phase=='writer'
    return result
