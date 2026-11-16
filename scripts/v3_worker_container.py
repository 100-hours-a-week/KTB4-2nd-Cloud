"""W1K: fixed-node privileged daemon reuse and observed container/lease evidence.

No AWS calls, Secret values, application writes or lease modification. Plans only
unless explicit apply/run commands are used. An API record is not SIGKILL proof.
"""
import copy
import json
import re
import time
from pathlib import Path
import v3_cdc_fault as f
import v3_worker_chaos as chaos

DAEMON = 'chaos-daemon'
NS = 'chaos-mesh'
BUDGET = {'requests': {'cpu':'50m','memory':'64Mi'}, 'limits': {'cpu':'300m','memory':'128Mi'}}
TOLERATIONS = [{'key':'yeodam.io/role','operator':'Equal','value':'worker','effect':'NoSchedule'}]


def daemon(k):
    d = k.get('daemonset', NS, DAEMON)
    template = d['spec']['template']['spec']
    containers = template['containers']
    lock = json.loads((f.CHART/'upstream.lock.json').read_text())
    f.require(len(containers)==1 and containers[0]['image']==lock['images']['chaos-daemon']
              and containers[0]['resources']==BUDGET, 'Keep pinned minimum daemon image/resources')
    f.require(template.get('hostPID') is True and not template.get('hostNetwork')
              and containers[0].get('securityContext',{}).get('privileged') is True,
              'Reviewed privileged runtime daemon required')
    f.require(any(v.get('hostPath',{}).get('path')=='/run/containerd' for v in template.get('volumes',[])),
              'Only reviewed containerd socket supported')
    args=containers[0].get('command',[])+containers[0].get('args',[])
    f.require('--runtime' in args and args[args.index('--runtime')+1]=='containerd' and
              '--runtime-socket-path' in args and args[args.index('--runtime-socket-path')+1]=='/host-run/containerd.sock' and
              '/etc/chaos-daemon/cert/tls.crt' in args and '/etc/chaos-daemon/cert/ca.crt' in args, 'Preserve runtime and mTLS arguments')
    # Privilege is host-wide. Narrow scheduling is NOT a per-container runtime ACL.
    return d


def target_node(k, victim, check_capacity=True):
    node = k.get('node', name=victim['node'])
    f.require(node['metadata']['labels'].get('yeodam.io/role')=='worker' and
              not node.get('spec',{}).get('unschedulable') and
              any(c['type']=='Ready' and c['status']=='True' for c in node['status']['conditions']),
              'Reviewed existing Ready CPU Worker required')
    f.require(node['metadata']['labels'].get('node.kubernetes.io/instance-type')in('t3a.medium','t3.medium') and
              not any(key.startswith('nvidia.com/')for key in node['status'].get('allocatable',{})), 'Existing minimal CPU instance only; GPU forbidden')
    info=node['status']['nodeInfo']
    f.require(info.get('operatingSystem')=='linux' and info.get('containerRuntimeVersion','').startswith('containerd://'),
              'Linux/containerd Worker only; no GPU target')
    f.require(re.fullmatch(r'ip-10-40-\d{1,3}-\d{1,3}(?:\.ap-northeast-2\.compute\.internal)?',node['metadata']['name']),
              'Existing Seoul V3 host required')
    if check_capacity:
        pods=k.get('pods','photo',selector='app.kubernetes.io/instance=photo-converter')['items']
        f.require(any(p['metadata']['uid']==victim['uid'] and p['spec'].get('nodeName')==victim['node'] for p in pods),
                  'Victim moved before daemon review')
        all_pods=json.loads(k.call(['get','pods','-A','-o','json']))['items']
        used=sum(p['spec'].get('nodeName')==victim['node'] and p['status'].get('phase')not in ('Succeeded','Failed') for p in all_pods)
        f.require(used<int(node['status']['allocatable']['pods']), 'No existing Worker Pod slot; do not add capacity')
    f.require(f.UUID.fullmatch(node['metadata']['uid']), 'Pinned Node UID required')
    return {'name':node['metadata']['name'],'uid':node['metadata']['uid'],'runtime':info['containerRuntimeVersion']}


def after_spec(before, node_name):
    result=copy.deepcopy(before)
    spec=result['template']['spec']
    f.require(not spec.get('affinity') and not spec.get('nodeName'), 'Review existing daemon affinity before retargeting')
    spec['nodeSelector']={'yeodam.io/role':'worker','kubernetes.io/hostname':node_name}
    spec['tolerations']=copy.deepcopy(TOLERATIONS)
    return result


def daemon_plan(k,snapshot):
    import v3_worker_fault as w
    w.validate_snapshot(snapshot); w.runtime(k,{'snapshot':snapshot})
    d=daemon(k); spec=d['spec']['template']['spec']
    f.require(spec['nodeSelector'].get('yeodam.io/role')=='system' and len(spec['nodeSelector'])==2 and
              d['status'].get('desiredNumberScheduled')==d['status'].get('numberReady')==1,
              'Start with the existing one-system-host daemon')
    f.require(chaos.env_map(chaos.controller(k)).get('TARGET_NAMESPACE')=='kafka', 'Prepare daemon from normal Kafka profile')
    source_node=k.get('node',name=spec['nodeSelector']['kubernetes.io/hostname'])
    f.require(source_node['metadata']['labels'].get('yeodam.io/role')=='system' and f.UUID.fullmatch(source_node['metadata']['uid']),'Pin original system Node UID')
    node=target_node(k,snapshot['victim'])
    value={'version':1,'clusterUid':k.identity(),'createdAt':f.now(),'node':node,'originalNode':{'name':source_node['metadata']['name'],'uid':source_node['metadata']['uid']},
           'victim':snapshot['victim'],'daemon':chaos.fields(d),'afterSpec':after_spec(d['spec'],node['name']),
           'privilege':'existing privileged hostPID/containerd daemon; host-wide authority on one temporary CPU host',
           'addedNodes':0,'addedDaemonSets':0,'desiredDaemonPods':1}
    return dict(value,planSha256=f.sha(value))


def verify_daemon(p,expected,fresh=False):
    import v3_worker_fault as w
    f.require(p['version']==1 and p['planSha256']==expected==f.sha({k:v for k,v in p.items()if k!='planSha256'}), 'Daemon Plan hash mismatch')
    f.require(p['addedNodes']==p['addedDaemonSets']==0 and p['desiredDaemonPods']==1 and
              f.UUID.fullmatch(p['node']['uid']) and p['node']['name']==p['victim']['node'] and
              p['afterSpec']==after_spec(p['daemon']['spec'],p['node']['name']), 'Only one reviewed CPU host retarget allowed')
    f.require(f.UUID.fullmatch(p['originalNode']['uid']) and p['originalNode']['name']==p['daemon']['spec']['template']['spec']['nodeSelector']['kubernetes.io/hostname'],'Original system host identity required')
    # Bind unchanged TLS volume references, args, image, security context and resources.
    fake={'metadata':{'uid':p['daemon']['uid']},'spec':p['daemon']['spec']}
    class Offline:
        def get(self,*args,**kwargs):return fake
    daemon(Offline())
    f.require(fake['spec']['template']['spec']['nodeSelector'].get('yeodam.io/role')=='system','Original system daemon required')
    if fresh:f.require(0<=time.time()-w.epoch(p['createdAt'])<=300,'Refresh daemon Plan within five minutes')


def patch_daemon(k,p,before,after):
    d=daemon(k)
    f.require(d['metadata']['uid']==p['daemon']['uid'] and d['spec']==before,'Daemon changed; never overwrite another edit')
    k.call(['-n',NS,'patch','daemonset',DAEMON,'--type=json','-p',json.dumps([
        {'op':'test','path':'/metadata/uid','value':d['metadata']['uid']},
        {'op':'test','path':'/metadata/resourceVersion','value':d['metadata']['resourceVersion']},
        {'op':'test','path':'/spec','value':before},{'op':'replace','path':'/spec','value':after}])])


def daemon_pods(k,d):
    selector=','.join(k+'='+v for k,v in sorted(d['spec']['selector']['matchLabels'].items()))
    return k.get('pods',NS,selector=selector)['items']


def wait_daemon(k,p,node_name,limit=120):
    end=time.monotonic()+limit
    while time.monotonic()<end:
        d=daemon(k); pods=daemon_pods(k,d)
        active=[x for x in pods if x['status'].get('phase')not in ('Succeeded','Failed')]
        if (d['status'].get('observedGeneration')==d['metadata']['generation'] and
            d['status'].get('desiredNumberScheduled')==d['status'].get('numberReady')==1 and
            len(active)==1 and any(o.get('controller') and o.get('kind')=='DaemonSet' and o.get('uid')==d['metadata']['uid'] for o in active[0]['metadata'].get('ownerReferences',[])) and active[0]['spec'].get('nodeName')==node_name and f.ready_pod(active[0])):
            return {'uid':active[0]['metadata']['uid'],'node':node_name}
        time.sleep(2)
    raise f.GuardError('Retargeted daemon not Ready; restore before input')


def apply_daemon(k,p,expected,path):
    verify_daemon(p,expected,True)
    f.require(k.identity()==p['clusterUid'] and chaos.fields(daemon(k))==p['daemon'], 'Wrong cluster or changed daemon')
    f.require(not k.get('networkchaos','kafka')['items'] and not k.get('podchaos','photo')['items'],'No concurrent fault during daemon move')
    f.require(target_node(k,p['victim'])==p['node'],'CPU Node UID/runtime changed')
    r={'plan':p,'changeRequested':True,'complete':False}
    path=Path(path);f.private_write(path,r) # journal before patch; never infer that a timeout did nothing
    try:
        patch_daemon(k,p,p['daemon']['spec'],p['afterSpec'])
        r['pod']=wait_daemon(k,p,p['node']['name']);r['complete']=True
    except BaseException:
        try:r['restore']=restore_daemon(k,r)
        finally:save_receipt(path,r)
        raise
    save_receipt(path,r);return r


def save_receipt(path,r):
    path=Path(path); tmp=path.with_suffix('.tmp')
    f.require(not path.is_symlink() and not tmp.exists() and path.parent.stat().st_mode&0o077==0,'Private daemon receipt required')
    f.private_write(tmp,r);tmp.replace(path)


def require_daemon(k,r,p=None):
    dp=r['plan'];verify_daemon(dp,dp['planSha256'])
    f.require(r.get('complete') and k.identity()==dp['clusterUid'],'Reviewed daemon receipt required')
    if p:f.require(p.get('daemonPlan')==dp and p['snapshot']['victim']==dp['victim'],'Daemon target/Plan mismatch')
    d=daemon(k);f.require(d['metadata']['uid']==dp['daemon']['uid'] and d['spec']==dp['afterSpec'],'Daemon retarget changed')
    node=target_node(k,dp['victim'],False);f.require(node==dp['node'],'Daemon host UID/runtime changed')
    pods=daemon_pods(k,d)
    f.require(len(pods)==1 and pods[0]['metadata']['uid']==r['pod']['uid'] and
              pods[0]['spec']['nodeName']==dp['node']['name'] and any(o.get('controller') and o.get('kind')=='DaemonSet' and o.get('uid')==dp['daemon']['uid'] for o in pods[0]['metadata'].get('ownerReferences',[])) and f.ready_pod(pods[0]),'Pinned daemon Pod must be Ready on victim host')
    slices=k.get('endpointslices',NS)['items']
    matches=[ep for es in slices if es['metadata']['name'].startswith('chaos-daemon-')
             for ep in es.get('endpoints',[]) if ep.get('nodeName')==dp['node']['name'] and
             pods[0]['status'].get('podIP') in ep.get('addresses',[]) and ep.get('conditions',{}).get('ready')is not False
             and ep.get('conditions',{}).get('terminating')is not True]
    f.require(len(matches)==1,'Pinned daemon EndpointSlice not published; do not inject')


def restore_daemon(k,r):
    p=r['plan'];verify_daemon(p,p['planSha256']);f.require(k.identity()==p['clusterUid'],'Wrong daemon restoration cluster')
    f.require(not k.get('podchaos','photo')['items'] and not k.get('networkchaos','kafka')['items'],'Finalize faults before daemon restoration')
    original=k.get('node',name=p['originalNode']['name'])
    f.require(original['metadata']['uid']==p['originalNode']['uid'] and original['metadata']['labels'].get('yeodam.io/role')=='system','Original system Node changed; manual daemon restore required')
    d=daemon(k)
    if d['spec']==p['afterSpec']:patch_daemon(k,p,p['afterSpec'],p['daemon']['spec'])
    else:f.require(d['metadata']['uid']==p['daemon']['uid'] and d['spec']==p['daemon']['spec'],'Concurrent daemon edit; manual restoration required')
    host=p['daemon']['spec']['template']['spec']['nodeSelector']['kubernetes.io/hostname']
    wait_daemon(k,p,host)
    return {'restored':True,'at':f.now(),'sameDaemonSet':True,'originalHost':host}


def termination(p,pod):
    """API disappearance, OOM137 or another container's termination never qualify."""
    victim=p['snapshot']['victim']
    if not pod or pod['metadata']['uid']!=victim['uid']:return None
    for status in pod.get('status',{}).get('containerStatuses',[]):
        if status.get('name')!='backend':continue
        for key in ('state','lastState'):
            end=status.get(key,{}).get('terminated',{})
            if (end.get('containerID')==victim['containerId'] and end.get('exitCode')==137 and
                end.get('signal',0)in(0,9) and end.get('reason')!='OOMKilled' and end.get('finishedAt')):
                return {'source':key+'.terminated','killedContainerId':end['containerID'],
                        'exitCode':137,'signal':end.get('signal'),'reason':end.get('reason'),
                        'finishedAtNodeClock':end['finishedAt'],'podUid':pod['metadata']['uid'],
                        'currentContainerId':status.get('containerID'),'restartCount':status.get('restartCount')}
    return None


def post_kill_window(selected,proof):
    """DB read must happen AFTER verified termination, not before the API read."""
    rows=proof['rows'];task=next((x for x in rows if x['section']=='worker_task'and x['taskId']==selected['taskId']),None)
    item=next((x for x in rows if x['section']=='worker_item'and x['itemId']==selected['currentItem']),None)
    return bool(task and item and task['status']=='RUNNING' and task['claimHash']==selected['claimHash'] and
            isinstance(task['leaseSeconds'],(int,float)) and task['leaseSeconds']>0 and
            item['saved']==0 and item['attempt']==selected['currentAttempt'])


def replacement(p,pod):
    old=p['snapshot']['victim']
    return any(x.get('name')=='backend' and x.get('containerID') and x['containerID']!=old['containerId']
               for x in pod.get('containerStatuses',[]))


def new_owner(selected,pods,owner):
    # A restarted container can keep its Pod IP; old Kafka membership is not recovery.
    return any(m.get('memberHash')!=selected['memberHash'] and re.fullmatch('[a-f0-9]{64}',m.get('memberHash',''))
               and m['host'].lstrip('/')==pod.get('ip') and selected['delivery']['partition']in m['partitions']
               for pod in pods for m in owner.get('members',[]))


def lease_timing(result,receipt,selected,samples,runtime):
    """Reconstruct expiry and new claim bounds; a released lease is a different path."""
    import v3_worker_fault as w
    def interval(sample):
        clock=w.epoch(next(x['utc']for x in sample['rows']if x['section']=='db_clock'))
        lo,hi=sample['dbMinusLocalIntervalSeconds'];return[clock-hi,clock-lo]
    ends=[r for r in runtime if (r.get('processTermination')or{}).get('exitCode')==137 and
          r['processTermination']['killedContainerId']==receipt.get('victimContainerId')]
    ended_at=w.epoch(ends[0]['at'])if ends else None
    post=[s for s in samples if ended_at is not None and s.get('queryStartedAt') and
          w.epoch(s['queryStartedAt'])>=ended_at and post_kill_window(selected,s)]
    old=None;new=None;prior=None;released=False
    for sample in samples:
        if not w.input_accepted(sample['rows']):continue
        task=next(x for x in sample['rows']if x['section']=='worker_task'and x['taskId']==selected['taskId'])
        at=interval(sample)
        if task['claimHash']==selected['claimHash'] and new is None:old=(sample,task,at)
        elif new is None and task['claimHash']:new=(sample,task,at);prior=old
        elif new is None and ended_at is not None and w.epoch(sample['observedAt'])>=ended_at and not task['claimHash']:
            released=True
    result.update(case='W1K',selectedItemId=selected['currentItem'],selectedAttempt=selected['currentAttempt'],
                  selectedProofSha256=selected['proofSha256'],processKillObserved=bool(ends and receipt.get('chaosInjectedObserved')),
                  postKillWindowObserved=bool(post and receipt.get('postKillWindowObserved')),
                  leaseReleaseObserved=released,expiryOrdering='not_assessed',
                  precision='DB clock intervals; node exit timestamp retained separately; no point claim-time fabrication')
    # Compatibility field consumed by common business assessment, NOT API disappearance.
    result['podKillObserved']=result['processKillObserved']
    if new and prior and prior[1]['leaseUntil'] and result['events'].get('consumerAssignedAt'):
        sample,task,old_at=prior;offset=sample['dbMinusLocalIntervalSeconds'];lease=w.epoch(task['leaseUntil'])
        lease_bounds=[lease-offset[1],lease-offset[0]];claim=[old_at[0],new[2][1]]
        previous=w.epoch(receipt['requestAt']);assigned=None
        for rt in runtime:
            at=w.epoch(rt['at'])
            if rt['at']==result['events']['consumerAssignedAt']:assigned=[previous,at];break
            previous=at
        f.require(assigned is not None,'Consumer assignment event missing from recorded runtime')
        eligible=[max(lease_bounds[0],assigned[0]),max(lease_bounds[1],assigned[1])]
        delay=[max(0,claim[0]-eligible[1]),max(0,claim[1]-eligible[0])]
        result.update(leaseExpiryBounds=[w.iso(x)for x in lease_bounds],eligibilityBounds=[w.iso(x)for x in eligible],
                      eligibilityAt=w.iso(eligible[0]),newClaimAt=w.iso(claim[1]),claimAtBounds=[w.iso(x)for x in claim],
                      eligibilityToClaimBounds=delay,prematureClaimObserved=claim[1]<lease_bounds[0],
                      claimIntervalOverlapsExpiry=claim[0]<lease_bounds[1]<=claim[1],
                      expiryOrdering='observed_early'if claim[1]<lease_bounds[0]else'consistent_at_observer_resolution')
        result['events'].update(leaseUntil=w.iso(lease_bounds[1]),newClaimAt=w.iso(claim[1]))
        if result.get('completedAtBounds'):
            done=[w.epoch(x)for x in result['completedAtBounds']]
            result['claimToCompletionBounds']=[max(0,done[0]-claim[1]),max(0,done[1]-claim[0])]
    return result
