"""Fixed-scope read-only scaling evidence. No traffic, mutation or secret APIs."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import time
from datetime import datetime, timezone

CLUSTER_UID = '46b80c44-56f4-416d-9f9b-e8b7b4cdc6a9'
HPA_TARGETS = [('frontend','frontend'),('auth','auth-api'),('trip','trip-api'),
               ('photo','photo-api'),('photo','keda-hpa-photo-converter')]
SHA = re.compile(r'[a-f0-9]{64}')
LIMIT = 4 * 1024 * 1024


def require(value, message):
    if not value: raise ValueError(message)


def utc():
    return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def load(path):
    p=Path(path)
    require(not p.is_symlink() and p.stat().st_size<=LIMIT,'Use a bounded regular evidence file')
    return json.loads(p.read_text())


def save(path, value):
    path=Path(path)
    require(path.parent.is_dir() and not path.parent.is_symlink() and path.parent.stat().st_mode&0o077==0,'Use an existing private directory (700)')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f: json.dump(value,f,indent=2,allow_nan=False);f.write('\n')


def input_manifest(root, selection, inspection=None, expected=20):
    """Hash selected JPEGs in order; never copy/decode photos or export raw EXIF."""
    root=Path(root);require(root.is_dir() and not root.is_symlink(),'Use an explicit local photo directory')
    root=root.resolve();paths=load(selection)
    require(type(expected)is int and 1<=expected<=1000 and isinstance(paths,list) and len(paths)==expected and len(set(paths))==len(paths),'Explicit unique selection and expected count required')
    metadata={}
    if inspection:
        source=load(inspection)
        require(source.get('inspectionSource')=='macOS ImageIO','Unknown local inspection source')
        for row in source['files']:
            require(row['relativePath'] not in metadata,'Duplicate inspection entry')
            metadata[row['relativePath']]=row
    files=[];seen=set();total=0
    for name in paths:
        require(isinstance(name,str) and not any(ord(c)<32 for c in name),'Invalid relative photo path')
        rel=PurePosixPath(name)
        require(not rel.is_absolute() and '..'not in rel.parts and str(rel)==name and rel.suffix.lower()in {'.jpg','.jpeg'},'Select relative JPEG files only')
        path=root/name
        require(path.resolve()==path and path.is_file() and not path.is_symlink(),'Photo symlinks are not accepted')
        h=hashlib.sha256()
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as f:
            before=os.fstat(f.fileno());require(0<before.st_size<=32*1024**2,'Photo exceeds bounded local input size')
            require(f.read(3)==b'\xff\xd8\xff','JPEG signature missing');f.seek(0)
            while block:=f.read(1024*1024): h.update(block)
            after=os.fstat(f.fileno());require((before.st_size,before.st_mtime_ns)==(after.st_size,after.st_mtime_ns),'Photo changed while hashing')
        sha=h.hexdigest();require(sha not in seen,'Duplicate photo content in baseline input');seen.add(sha);total+=after.st_size
        require(total<=2*1024**3,'Input set exceeds bounded size')
        row={'index':len(files),'relativePath':name,'sha256':sha,'bytes':after.st_size,'format':'jpeg'}
        if inspection:
            info=metadata.get(name,{})
            require(info.get('sha256')==sha and info.get('bytes')==after.st_size,'Inspection does not match current photo bytes')
            require(type(info.get('hasGPS'))is bool and type(info.get('hasCaptureTime'))is bool,'Missing inspected metadata flags')
            require(all(type(info.get(k))is int and info[k]>0 for k in ['width','height']),'Invalid inspected dimensions')
            row['inspection']={k:info[k]for k in ['hasGPS','hasCaptureTime','width','height']}
        files.append(row)
    return {'schemaVersion':1,'kind':'v3-private-photo-input','root':str(root),'files':files,
            'inputSetSha256':digest([{'sha256':x['sha256'],'bytes':x['bytes']}for x in files]),
            'fileCount':len(files),'totalBytes':total,'imageDecoded':False,
            'metadataSource':'provided ImageIO inspection bound to file hash'if inspection else 'not_inspected'}


def input_summary(path):
    """Only aggregate input identity enters metric/timeline artifacts."""
    if not path: return None
    raw=Path(path).read_bytes();require(len(raw)<=LIMIT and not Path(path).is_symlink(),'Invalid input manifest')
    data=json.loads(raw);require(data.get('kind')=='v3-private-photo-input' and data.get('schemaVersion')==1,'Unknown input manifest')
    rows=data['files'];require(isinstance(rows,list) and 1<=len(rows)<=1000,'Invalid input count')
    require(all(SHA.fullmatch(x['sha256']) and type(x['bytes'])is int and 0<x['bytes']<=32*1024**2 and x['index']==i and x['format']=='jpeg'for i,x in enumerate(rows)),'Invalid input identity')
    identity=digest([{'sha256':x['sha256'],'bytes':x['bytes']}for x in rows])
    require(identity==data['inputSetSha256'] and len(rows)==data['fileCount'] and sum(x['bytes']for x in rows)==data['totalBytes'],'Input manifest totals/hash changed')
    inspected=[x['inspection']for x in rows if 'inspection'in x]
    require(all(type(x.get('hasGPS'))is bool and type(x.get('hasCaptureTime'))is bool for x in inspected),'Invalid input metadata coverage')
    return {'inputSetSha256':identity,'manifestSha256':hashlib.sha256(raw).hexdigest(),'fileCount':len(rows),'totalBytes':data['totalBytes'],
            'inspectedFiles':len(inspected),'gpsPresentFiles':sum(x['hasGPS']for x in inspected),'captureTimePresentFiles':sum(x['hasCaptureTime']for x in inspected),
            'metadataSource':data['metadataSource']}


def identity(read):
    require(read('get','namespace','kube-system')['metadata']['uid']==CLUSTER_UID,'Wrong V3 cluster')


def node_class(read, pool):
    ref=pool['spec']['template']['spec']['nodeClassRef']
    require(ref['group']=='karpenter.k8s.aws' and ref['kind']=='EC2NodeClass' and re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,62}',ref['name']),'Unknown NodeClass reference')
    return read('get','ec2nodeclass',ref['name'])


def policy_record(obj):
    # Hash specs, including possible future userData; never export their raw values.
    return {'uid':obj['metadata']['uid'],'specSha256':digest(obj['spec'])}


def policies(read):
    identity(read);result={}
    for ns,name in HPA_TARGETS:
        result['hpa/'+ns+'/'+name]=policy_record(read('-n',ns,'get','hpa',name))
    result['scaledobject/photo/photo-converter']=policy_record(read('-n','photo','get','scaledobject','photo-converter'))
    for name in ['api','worker']:
        pool=read('get','nodepool',name);result['nodepool/'+name]=policy_record(pool)
        cls=node_class(read,pool);result['ec2nodeclass/'+cls['metadata']['name']]=policy_record(cls)
    return result


def compare_conditions(before, after):
    def apps(s):
        return [{k:v for k,v in x.items()if k!='replicas'}for x in s['apps']]
    app_match=apps(before)==apps(after) and before['approvedRevision']==after['approvedRevision']
    p1,p2=before.get('scalingPolicies'),after.get('scalingPolicies')
    policy_match=p1==p2 if p1 and p2 else None
    a={x['name']:x.get('replicas')for x in before['apps']};b={x['name']:x.get('replicas')for x in after['apps']}
    return {'appConfigurationMatch':app_match,'scalingPolicyMatch':policy_match,
            'staticConditionsMatch':app_match and policy_match if policy_match is not None else None,
            'replicaChanges':{k:{'before':a.get(k),'after':b.get(k)}for k in sorted(set(a)|set(b))if a.get(k)!=b.get(k)},
            'nodeTypesChanged':before['nodeTypes']!=after['nodeTypes'],
            'capacityChangeIsNotAutomaticallyApproved':True,'endpointComparisonOnly':True}


def conditions(obj, allowed):
    result={}
    for c in obj.get('status',{}).get('conditions',[]):
        if c['type']in allowed:
            require(c['status']in ['True','False','Unknown'],'Unknown condition status')
            row={'status':c['status']}
            if c.get('lastTransitionTime'):row['transitionAt']=c['lastTransitionTime']
            result[c['type']]=row
    return result


def instance_id(value):
    match=re.fullmatch(r'aws:///ap-northeast-2[a-d]/(i-[a-f0-9]{17})',value or '')
    return match.group(1)if match else None


def poll(read):
    started=utc();dep=read('-n','photo','get','deployment','photo-converter')
    require(dep['spec']['selector']['matchLabels'].get('app.kubernetes.io/instance')=='photo-converter','Unexpected converter selector')
    h=read('-n','photo','get','hpa','keda-hpa-photo-converter');so=read('-n','photo','get','scaledobject','photo-converter')
    require(h['spec']['scaleTargetRef']['name']==so['spec']['scaleTargetRef']['name']=='photo-converter','Wrong scaling target')
    require(h['spec']['minReplicas']==so['spec']['minReplicaCount']==1 and h['spec']['maxReplicas']==so['spec']['maxReplicaCount']==2,'Keep reviewed 1-to-2 bounds')
    require(any(x.get('uid')==so['metadata']['uid']for x in h['metadata'].get('ownerReferences',[])),'HPA owner differs from ScaledObject')
    pods=read('-n','photo','get','pods','-l','app.kubernetes.io/instance=photo-converter')['items']
    nodes=read('get','nodes','-l','karpenter.sh/nodepool=worker')['items'];claims=read('get','nodeclaims','-l','karpenter.sh/nodepool=worker')['items']
    require(len(pods)<=10 and len(nodes)<=10 and len(claims)<=10,'Unexpected evidence scope size')
    pool=read('get','nodepool','worker');cls=node_class(read,pool)
    a=read('-n','argocd','get','application','yeodam-v3-apps');revision=a.get('status',{}).get('sync',{}).get('revision')
    require(re.fullmatch('[a-f0-9]{40}',revision or ''),'Approved revision unavailable')
    def common(o):return {'name':o['metadata']['name'],'uid':o['metadata']['uid'],'createdAt':o['metadata'].get('creationTimestamp'),'terminatingAt':o['metadata'].get('deletionTimestamp')}
    def worker(o):
        require(o['metadata'].get('labels',{}).get('karpenter.sh/nodepool')=='worker','Unexpected non-worker resource')
        return dict(common(o),instanceId=instance_id(o.get('spec',{}).get('providerID')or o.get('status',{}).get('providerID')),conditions=conditions(o,{'Ready','Launched','Registered','Initialized','Consolidatable'}))
    rows=[]
    for p in pods:
        row=dict(common(p),node=p['spec'].get('nodeName'),phase=p.get('status',{}).get('phase'),conditions=conditions(p,{'Ready'}),restarts=sum(c.get('restartCount',0)for c in p.get('status',{}).get('containerStatuses',[])))
        rows.append(row)
    observed=dep.get('status',{}).get('observedGeneration',-1)>=dep['metadata']['generation']
    result={'sampleStartedAt':started,'observedAt':utc(),'approvedRevision':revision,
            'policySha256':digest({'hpa':policy_record(h),'scaledobject':policy_record(so),'worker':policy_record(pool),'class':policy_record(cls),'converterTemplate':digest(dep['spec']['template'])}),
            'hpa':{'current':h.get('status',{}).get('currentReplicas'),'desired':h.get('status',{}).get('desiredReplicas'),'conditions':conditions(h,{'AbleToScale','ScalingActive','ScalingLimited'})},
            'scaledObject':conditions(so,{'Ready','Active','Fallback','Paused'}),
            'converter':{'specReplicas':dep['spec']['replicas'],'statusObserved':observed,'available':dep.get('status',{}).get('availableReplicas',0)if observed else None,'ready':dep.get('status',{}).get('readyReplicas',0)if observed else None},
            'pods':sorted(rows,key=lambda x:x['uid']),'nodes':sorted([worker(x)for x in nodes],key=lambda x:x['uid']),
            'nodeClaims':sorted([worker(x)for x in claims],key=lambda x:x['uid'])}
    return result


def ready(row):
    return not row.get('terminatingAt') and row.get('conditions',{}).get('Ready',{}).get('status')=='True'


def summarize(rows):
    events=[];previous=None;first=None;sequence=[];distinct=False;has_gap=False;policy_changed=False
    for row in rows:
        if row.get('error'):
            has_gap=True;previous=None;continue
        if first is None:first=row
        policy_changed=policy_changed or any(first[k]!=row[k]for k in ['policySha256','approvedRevision'])
        n=row['converter']['ready']
        if n is None:has_gap=True
        if n is not None and (not sequence or n!=sequence[-1]):sequence.append(n)
        live=[p for p in row['pods']if ready(p)]
        if len(live)==2 and all(p.get('node')for p in live) and len({p['node']for p in live})==2:distinct=True
        if previous:
            def event(name, **extra):events.append(dict(event=name,notBefore=previous['sampleStartedAt'],observedBy=row['observedAt'],**extra))
            for group,field in [('hpa','desired'),('converter','ready')]:
                if previous[group][field]!=row[group][field]:event(group+'_'+field+'_changed',before=previous[group][field],after=row[group][field])
            for group in ['pods','nodes','nodeClaims']:
                a={x['uid']:x for x in previous[group]};b={x['uid']:x for x in row[group]}
                for uid in sorted(set(b)-set(a)):event(group+'_observed',uid=uid)
                for uid in sorted(set(a)-set(b)):event(group+'_removed_from_api',uid=uid)
                for uid in sorted(set(a)&set(b)):
                    if not ready(a[uid])and ready(b[uid]):event(group+'_ready',uid=uid)
            if previous['policySha256']!=row['policySha256']:event('policy_changed');policy_changed=True
            if previous['approvedRevision']!=row['approvedRevision']:event('approved_revision_changed');policy_changed=True
        previous=row
    good=[r for r in rows if not r.get('error')]
    baseline={r['instanceId']for group in ['nodes','nodeClaims']for r in good[0][group]if r.get('instanceId')}if good else set()
    last={r['instanceId']for group in ['nodes','nodeClaims']for r in good[-1][group]if r.get('instanceId')}if good else set()
    seen={r['instanceId']for s in good for group in ['nodes','nodeClaims']for r in s[group]if r.get('instanceId')}
    path=False
    for i,n in enumerate(sequence):
        if n==1 and 2 in sequence[i+1:]:
            j=sequence.index(2,i+1);path=path or 1 in sequence[j+1:]
    return {'events':events,'readyReplicaSequence':sequence,'oneTwoOneObserved':path,'continuousOneTwoOneObserved':path and not has_gap and not policy_changed,'twoReadyOnDistinctNodesObserved':distinct,
            'observationGaps':has_gap,'policyOrRevisionChanged':policy_changed,'removedNewWorkerInstanceIds':sorted(seen-baseline-last),
            'remainingNewWorkerInstanceIds':sorted((seen-baseline)&last),
            'ec2TerminationVerified':False,'businessCompletionVerified':False,'verdict':'not_assessed'}


def observe(read, output, experiment_id, duration, interval=15, input_identity=None):
    require(re.fullmatch('[A-Za-z0-9_-]{1,64}',experiment_id or ''),'Opaque experiment ID required')
    require(type(duration)is int and 60<=duration<=3600 and type(interval)is int and 10<=interval<=60,'Use a reviewed 1-to-60-minute observation with 10-to-60-second polling')
    identity(read);out=Path(output);out.mkdir(mode=0o700,parents=True,exist_ok=False);rows=[];started=utc();base=time.monotonic();next_at=base
    status='complete';reason=None
    fd=os.open(out/'samples.jsonl',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w')as f:
            while True:
                try:row=poll(read)
                except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):row={'sampleStartedAt':utc(),'observedAt':utc(),'error':'query_failed'};status='partial'
                rows.append(row);f.write(json.dumps(row,sort_keys=True,allow_nan=False)+'\n');f.flush()
                if time.monotonic()-base>=duration:break
                next_at+=interval;time.sleep(max(0,min(next_at-base,duration)-(time.monotonic()-base)))
    except KeyboardInterrupt:status='partial';reason='interrupted'
    result={'schemaVersion':1,'kind':'v3-scaling-observation','experimentId':experiment_id,'clusterUid':CLUSTER_UID,'startedAt':started,'finishedAt':utc(),
            'intervalSeconds':interval,'collectionStatus':status,'stopReason':reason,'sampleCount':len(rows),'input':input_identity,
            'samplesSha256':hashlib.sha256((out/'samples.jsonl').read_bytes()).hexdigest(),'scope':'photo-converter and worker NodePool only',**summarize(rows)}
    save(out/'result.json',result);return result


def verify_node_release(observation, run=subprocess.run):
    """Optional exact-ID AWS describe only; API absence is never termination proof."""
    path=Path(observation);meta=load(path);require(meta.get('kind')=='v3-scaling-observation' and meta.get('clusterUid')==CLUSTER_UID,'Unknown scaling observation')
    raw_path=path.parent/'samples.jsonl';require(not raw_path.is_symlink() and raw_path.stat().st_size<=LIMIT,'Invalid raw observation')
    raw=raw_path.read_bytes();require(hashlib.sha256(raw).hexdigest()==meta['samplesSha256'],'Raw observation changed')
    facts=summarize([json.loads(x)for x in raw.splitlines()]);ids=facts['removedNewWorkerInstanceIds']
    require(len(ids)<=4 and all(re.fullmatch('i-[a-f0-9]{17}',x)for x in ids),'Unexpected instance scope')
    remaining=facts['remainingNewWorkerInstanceIds']
    if not ids:return {'observedAt':utc(),'instanceStates':{},'remainingNewWorkerInstanceIds':remaining,'allRecordedCandidatesTerminated':None,'allNewWorkersTerminated':None,'status':'no_removed_new_worker_observed'}
    def aws(args):
        p=run(['aws','--profile','yeodam-admin','--region','ap-northeast-2',*args,'--output','json'],capture_output=True,text=True,timeout=25)
        require(p.returncode==0,'Read-only AWS verification unavailable');return json.loads(p.stdout)
    require(aws(['sts','get-caller-identity']).get('Account')=='483175530259','Wrong AWS account')
    result=aws(['ec2','describe-instances','--instance-ids',*ids,'--filters','Name=tag:karpenter.sh/nodepool,Values=worker','Name=tag:Version,Values=v3','Name=tag:Project,Values=yeodam'])
    states={x:'not_observed'for x in ids}
    for reservation in result.get('Reservations',[]):
        for inst in reservation['Instances']:
            require(inst['InstanceId']in ids,'Unexpected instance response');states[inst['InstanceId']]=inst['State']['Name']
    terminated=all(v=='terminated'for v in states.values())
    return {'observedAt':utc(),'instanceStates':states,'remainingNewWorkerInstanceIds':remaining,
            'allRecordedCandidatesTerminated':terminated,'allNewWorkersTerminated':terminated and not remaining,
            'status':'observed','exactTerminationTimeKnown':False,'lifecycleChanges':False}
