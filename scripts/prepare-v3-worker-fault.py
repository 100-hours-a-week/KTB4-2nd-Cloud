#!/usr/bin/env python3
"""Prepare W1 offline; live profile changes and one-shot kill require distinct approvals.

No subcommand starts/stops a GPU, creates a trip, uploads photos, modifies SQL,
commits an offset, or changes application spec/resources. Use mode-700 outputs.
"""
import argparse
import base64
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import v3_cdc_fault as f
import v3_worker_fault as w
import v3_worker_chaos as chaos
import v3_worker_container as hard


def load(path):
    f.require(path.is_file() and not path.is_symlink() and path.stat().st_size<=4*1024*1024,'Bounded regular input file required')
    return json.loads(path.read_text())


def ownership(k,name,uid,deadline=None):
    k=w.DeadlineKube(k);k.deadline=deadline
    p=k.get('pod','keda',name)
    f.require(p['metadata']['uid']==uid and p['metadata'].get('labels',{}).get('yeodam.io/owner')=='worker-fault-preparation', 'Wrong observer Pod')
    raw=k.call(['-n','keda','logs',name,'-c','probe','--tail=30'])
    rows=[f.bounded_json(line.removeprefix('WORKER_OWNERSHIP ')) for line in raw.splitlines() if line.startswith('WORKER_OWNERSHIP ')]
    f.require(rows,'Kafka ownership unavailable')
    if rows[-1].get('unavailable'):
        kind=rows[-1].get('errorType','')
        suffix=' ('+kind+')' if re.fullmatch(r'[A-Za-z][A-Za-z0-9]{0,79}',kind)else''
        raise f.GuardError('Kafka ownership unavailable'+suffix)
    value=rows[-1]
    started=time.time();mono=time.monotonic()
    raw=k.call(['-n','keda','exec',name,'-c','probe','--','date','+%s.%N'])
    ended=time.time();elapsed=time.monotonic()-mono
    f.require(re.fullmatch(r'[0-9]{10}\.[0-9]{1,9}',raw.strip()),'Observer UTC reference unavailable')
    value['clock']={'kind':'observer-pod-utc','podUid':uid,'nodeUnixSeconds':float(raw.strip()),
                    'localStartedUnixSeconds':started,'localCompletedUnixSeconds':ended,'monotonicElapsedSeconds':elapsed}
    w.ownership_age_bounds(value,ended)
    return value


# Read the atomically published frame BEFORE the node clock. Downward API binds
# the exec to the expected observer identity without a separate API GET per poll.
FAST_FRAME_COMMAND = ['sh','-ec',
    'printf "POD_UID %s\\n" "$W1_OBSERVER_POD_UID"; cat /tmp/worker-ownership.json; printf "NODE_UTC %s\\n" "$(date +%s.%N)"']


def fast_ownership(k,name,uid,deadline=None):
    f.require(w.NAME.fullmatch(name) and name.startswith('w1-') and f.UUID.fullmatch(uid),'Exact observer identity required')
    k=w.DeadlineKube(k);k.deadline=deadline
    started=time.time();mono=time.monotonic()
    raw=k.call(['-n','keda','exec',name,'-c','probe','--']+FAST_FRAME_COMMAND)
    ended=time.time();elapsed=time.monotonic()-mono
    f.require(len(raw)<=256*1024,'Bounded ownership frame required')
    lines=raw.splitlines()
    f.require(len(lines)==3 and lines[0]=='POD_UID '+uid and lines[2].startswith('NODE_UTC '),'Wrong or incomplete observer frame')
    node=lines[2].removeprefix('NODE_UTC ')
    f.require(re.fullmatch(r'[0-9]{10}\.[0-9]{1,9}',node),'Observer UTC reference unavailable')
    value=f.bounded_json(lines[1]);f.require(isinstance(value,dict) and not value.get('unavailable'),'Kafka ownership unavailable')
    value['clock']={'kind':'observer-pod-utc','podUid':uid,'nodeUnixSeconds':float(node),
                    'localStartedUnixSeconds':started,'localCompletedUnixSeconds':ended,'monotonicElapsedSeconds':elapsed}
    w.ownership_age_bounds(value,ended)
    return value


def reader_node(node):
    # EKS uses the full private DNS name as metadata.name. Preserve that exact
    # identity; shortening it produces a different, nonexistent Kubernetes Node.
    match=re.fullmatch(r'ip-10-40-([0-9]{1,3})-([0-9]{1,3})(?:\.ap-northeast-2\.compute\.internal)?',node) if isinstance(node,str) else None
    f.require(match is not None and all(int(part)<=255 for part in match.groups()),'Exact existing V3 CPU host required')
    return node


def reader_bundle(jar,node,name):
    lock=json.loads((w.ROOT/'deploy/v3/autoscaling/converter-runtime.lock.json').read_text())
    f.require(jar.is_file() and hashlib.sha256(jar.read_bytes()).hexdigest()==lock['kafkaClientSha256'],'Pinned Kafka client jar required')
    f.require(w.NAME.fullmatch(name) and name.startswith('w1-'),'Exact temporary reader name required')
    reader_node(node)
    with tempfile.TemporaryDirectory() as tmp:
        p=subprocess.run([os.environ.get('JAVAC','javac'),'--release','17','-cp',str(jar),'-d',tmp,str(w.ROOT/'tests/ci/v3/worker/ConverterOwnership.java')],capture_output=True,timeout=30)
        f.require(p.returncode==0,'Kafka metadata reader compilation failed')
        binary=base64.b64encode((Path(tmp)/'ConverterOwnership.class').read_bytes()).decode()
    return reader_manifest(node,name,binary)


def reader_manifest(node,name,binary):
    reader_node(node)
    meta={'name':name,'namespace':'keda','labels':{'yeodam.io/owner':'worker-fault-preparation','yeodam.io/component':'converter-lag-probe'}}
    cm={'apiVersion':'v1','kind':'ConfigMap','metadata':meta,'binaryData':{'ConverterOwnership.class':binary}}
    image=json.loads((w.ROOT/'deploy/v3/connect-image/source.lock.json').read_text())['baseImage']
    pod={'apiVersion':'v1','kind':'Pod','metadata':meta,'spec':{'nodeName':node,'restartPolicy':'Never',
        'activeDeadlineSeconds':1050,'automountServiceAccountToken':False,
        'securityContext':{'runAsNonRoot':True,'runAsUser':1001,'runAsGroup':1001,'seccompProfile':{'type':'RuntimeDefault'}},
        'containers':[{'name':'probe','image':image,'command':['java','-Xmx64m','-cp','/probe:/opt/kafka/libs/*','ConverterOwnership','1020'],
          'env':[{'name':'W1_OBSERVER_POD_UID','valueFrom':{'fieldRef':{'fieldPath':'metadata.uid'}}}],
          'resources':{'requests':{'cpu':'25m','memory':'64Mi'},'limits':{'cpu':'250m','memory':'192Mi'}},
          'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},
          'volumeMounts':[{'name':'code','mountPath':'/probe','readOnly':True},{'name':'auth','mountPath':'/auth','readOnly':True},{'name':'tmp','mountPath':'/tmp'}]}],
        'volumes':[{'name':'code','configMap':{'name':name}},{'name':'auth','secret':{'secretName':'v3-keda-kafka','defaultMode':292}},{'name':'tmp','emptyDir':{'medium':'Memory','sizeLimit':'16Mi'}}]}}
    # Describe-only credentials are mounted where already provisioned; no Secret read/copy.
    policy={'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':meta,
        'spec':{'podSelector':{'matchLabels':meta['labels']},'policyTypes':['Ingress','Egress'],'ingress':[],
          'egress':[{'to':[{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':'kube-system'}},'podSelector':{'matchLabels':{'k8s-app':'kube-dns'}}}],
                     'ports':[{'protocol':'UDP','port':53},{'protocol':'TCP','port':53}]},
                    {'to':[{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':'kafka'}},'podSelector':{'matchLabels':{'strimzi.io/name':'yeodam-v3-kafka-kafka'}}}],
                     'ports':[{'protocol':'TCP','port':9093}]}]}}
    return {'apiVersion':'v1','kind':'List','items':[cm,policy,pod]}



def reader_start(k,bundle,expected,directory):
    f.require(f.sha(bundle)==expected and bundle['kind']=='List' and len(bundle['items'])==3,'Reader bundle hash mismatch')
    items=bundle['items'];meta=items[0]['metadata'];name=meta['name']
    f.require(w.NAME.fullmatch(name) and name.startswith('w1-') and all(x['metadata']==meta for x in items)
              and meta['namespace']=='keda' and meta['labels']=={'yeodam.io/owner':'worker-fault-preparation','yeodam.io/component':'converter-lag-probe'}
              and [x['kind']for x in items]==['ConfigMap','NetworkPolicy','Pod'],'Only exact temporary reader resources allowed')
    pod=items[-1];sp=pod['spec']
    f.require(bundle==reader_manifest(sp['nodeName'],name,items[0]['binaryData']['ConverterOwnership.class']), 'Only canonical read-only observer bundle permitted')
    node=k.get('node',name=sp['nodeName'])
    f.require(node['metadata']['labels'].get('yeodam.io/role') in ('system','worker') and
              any(x['type']=='Ready' and x['status']=='True' for x in node['status']['conditions']) and
              not node.get('spec',{}).get('unschedulable'),'Existing Ready CPU node only')
    used=len([x for x in f.bounded_json(k.call(['get','pods','-A','-o','json']))['items'] if x['spec'].get('nodeName')==sp['nodeName'] and x['status'].get('phase')not in ('Succeeded','Failed')])
    f.require(used<int(node['status']['allocatable']['pods']),'Existing node Pod slots exhausted')
    c=sp['containers'][0]
    f.require(len(sp['containers'])==1 and sp['automountServiceAccountToken'] is False and sp['activeDeadlineSeconds']==1050 and
              c['resources']=={'requests':{'cpu':'25m','memory':'64Mi'},'limits':{'cpu':'250m','memory':'192Mi'}} and
              c['securityContext']=={'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}} and
              not sp.get('hostNetwork') and not sp.get('hostPID') and all(v.get('secret',{}).get('secretName','v3-keda-kafka')=='v3-keda-kafka' for v in sp['volumes']) and
              c['image']==json.loads((w.ROOT/'deploy/v3/connect-image/source.lock.json').read_text())['baseImage'], 'Reader security/resource contract changed')
    directory=Path(directory);f.require(directory.is_dir() and directory.stat().st_mode&0o077==0 and not directory.is_symlink() and not any(directory.iterdir()),'Empty private reader directory required')
    receipt={'clusterUid':k.identity(),'name':name,'resources':[],'bundleSha256':expected,'at':f.now()}
    try:
        for obj in items:
            live=f.bounded_json(k.call(['create','-f','-','-o','json'],json.dumps(obj)))
            entry={'kind':obj['kind'],'name':name,'uid':live['metadata']['uid']};receipt['resources'].append(entry)
            f.private_write(directory/(obj['kind']+'.json'),entry)
        f.private_write(directory/'receipt.json',receipt);return receipt
    except Exception:
        f.private_write(directory/'partial-receipt.json',receipt);reader_stop(k,receipt);raise


def reader_stop(k,receipt):
    f.require(k.identity()==receipt['clusterUid'],'Wrong reader cleanup cluster')
    for entry in reversed(receipt['resources']):
        kind=entry['kind'];name=entry['name']
        f.require(kind in ('ConfigMap','NetworkPolicy','Pod') and w.NAME.fullmatch(name) and name.startswith('w1-'),'Wrong reader cleanup identity')
        obj=json.loads(k.call(['-n','keda','get',kind,name,'--ignore-not-found','-o','json']) or '{}')
        if not obj:continue
        f.require(obj['metadata']['uid']==entry['uid'] and obj['metadata'].get('labels',{}).get('yeodam.io/owner')=='worker-fault-preparation','Reader cleanup owner changed')
        path=('/apis/networking.k8s.io/v1' if kind=='NetworkPolicy' else '/api/v1')+'/namespaces/keda/'+{'Pod':'pods','ConfigMap':'configmaps','NetworkPolicy':'networkpolicies'}[kind]+'/'+name
        k.call(['delete','--raw',path,'-f','-'],json.dumps({'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':entry['uid']}}))
    return {'readerRemoved':True,'at':f.now()}


def guard_ready(path,plan):
    v=load(path)
    f.require(path.parent.stat().st_mode&0o077==0 and v.get('planSha256')==plan['planSha256'] and
              v.get('instanceId')=='i-05e1ad9dd96dba4ae' and v.get('independent') is True and
              type(v.get('pid')) is int and v['pid']>1 and 0<=time.time()-w.epoch(v['at'])<=60 and
              v.get('remainingSeconds',0)<=1500 and v.get('remainingSeconds',0)-(time.time()-w.epoch(v['at']))>=980,'Fresh independent GPU stop guard proof required')
    stop=Path(v.get('stopFile',''))
    f.require(stop.is_absolute() and stop.parent==path.parent and not stop.exists() and not stop.is_symlink(), 'Owned guard stop file required')
    os.kill(v['pid'],0) # Liveness check only; this process never invokes EC2 lifecycle APIs.
    return lambda:stop.touch(mode=0o600,exist_ok=False)


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    a=sub.add_parser('snapshot');a.add_argument('--kubeconfig',type=Path,required=True);a.add_argument('--output',type=Path,required=True)
    a=sub.add_parser('plan')
    for key in ['snapshot','input','output']:a.add_argument('--'+key,type=Path,required=True)
    a.add_argument('--failure-mode',choices=['pod-kill','container-kill'],default='pod-kill');a.add_argument('--daemon-plan',type=Path)
    a.add_argument('--trip-id',type=int,required=True);a.add_argument('--run-id',required=True);a.add_argument('--ack-maintenance',action='store_true',required=True)
    a=sub.add_parser('profile-plan');a.add_argument('--kubeconfig',type=Path,required=True);a.add_argument('--output',type=Path,required=True)
    a=sub.add_parser('profile-apply')
    a.add_argument('--kubeconfig',type=Path,required=True);a.add_argument('--plan',type=Path,required=True);a.add_argument('--approve-profile-sha256',required=True)
    a.add_argument('--receipt',type=Path,required=True);a.add_argument('--ack-chaos-controller-retarget',action='store_true',required=True)
    a=sub.add_parser('profile-restore');a.add_argument('--kubeconfig',type=Path,required=True);a.add_argument('--receipt',type=Path,required=True);a.add_argument('--output',type=Path,required=True)
    a=sub.add_parser('daemon-plan');a.add_argument('--kubeconfig',type=Path,required=True);a.add_argument('--snapshot',type=Path,required=True);a.add_argument('--output',type=Path,required=True)
    a=sub.add_parser('daemon-apply');a.add_argument('--kubeconfig',type=Path,required=True);a.add_argument('--plan',type=Path,required=True);a.add_argument('--approve-daemon-sha256',required=True);a.add_argument('--receipt',type=Path,required=True);a.add_argument('--ack-one-host-privileged-daemon',action='store_true',required=True)
    a=sub.add_parser('daemon-restore');a.add_argument('--kubeconfig',type=Path,required=True);a.add_argument('--receipt',type=Path,required=True);a.add_argument('--output',type=Path,required=True)
    a=sub.add_parser('reader-bundle');a.add_argument('--kafka-client',type=Path,required=True);a.add_argument('--node',required=True);a.add_argument('--name',required=True);a.add_argument('--output',type=Path,required=True)
    a=sub.add_parser('reader-start');a.add_argument('--kubeconfig',type=Path,required=True);a.add_argument('--bundle',type=Path,required=True);a.add_argument('--approve-bundle-sha256',required=True);a.add_argument('--directory',type=Path,required=True);a.add_argument('--ack-mounted-describe-credentials',action='store_true',required=True)
    a=sub.add_parser('reader-stop');a.add_argument('--kubeconfig',type=Path,required=True);a.add_argument('--receipt',type=Path,required=True);a.add_argument('--output',type=Path,required=True)
    a=sub.add_parser('collect');a.add_argument('--kubeconfig',type=Path,required=True);a.add_argument('--snapshot',type=Path,required=True);a.add_argument('--trip-id',type=int,required=True);a.add_argument('--directory',type=Path,required=True);a.add_argument('--ack-trial-metadata-read',action='store_true',required=True)
    a=sub.add_parser('sampling-probe')
    a.add_argument('--kubeconfig',type=Path,required=True);a.add_argument('--snapshot',type=Path,required=True)
    a.add_argument('--reader-receipt',type=Path,required=True);a.add_argument('--trip-id',type=int,required=True)
    a.add_argument('--directory',type=Path,required=True);a.add_argument('--ack-trial-metadata-read',action='store_true',required=True)
    a=sub.add_parser('run')
    a.add_argument('--kubeconfig',type=Path,required=True);a.add_argument('--plan',type=Path,required=True);a.add_argument('--approve-plan-sha256',required=True)
    a.add_argument('--profile-receipt',type=Path,required=True);a.add_argument('--directory',type=Path,required=True)
    a.add_argument('--reader-receipt',type=Path,required=True);a.add_argument('--gpu-guard',type=Path,required=True)
    a.add_argument('--daemon-receipt',type=Path);a.add_argument('--sampling-proof',type=Path)
    ack=a.add_mutually_exclusive_group(required=True)
    ack.add_argument('--ack-maintenance-and-one-pod-kill',action='store_true')
    ack.add_argument('--ack-maintenance-and-one-container-kill',action='store_true')
    a=sub.add_parser('assess')
    for key in ['before','after','timing','trace','db-summary','output']:a.add_argument('--'+key,type=Path,required=True)
    a=sub.add_parser('summarize');a.add_argument('--directory',type=Path,required=True);a.add_argument('--output',type=Path,required=True)
    a=sub.add_parser('dashboard');a.add_argument('--record',type=Path,required=True);a.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if hasattr(args,'output'):
        f.require(not args.output.exists() and not args.output.is_symlink() and args.output.parent.is_dir() and args.output.parent.stat().st_mode&0o077==0,'Fresh mode-700 output required')
    if args.action=='sampling-probe':
        s=load(args.snapshot);w.validate_snapshot(s);k=f.Kube(args.kubeconfig)
        live=w.snapshot(k)
        f.require(all(live[key]==s[key] for key in ('kubeSystemUid','approvedRevision','victim','mysql','apps')),'Sampling target changed')
        rr=load(args.reader_receipt);f.require(rr['clusterUid']==s['kubeSystemUid'],'Wrong reader cluster')
        pod=next(x for x in rr['resources']if x['kind']=='Pod')
        result=w.sampling_probe(k,s,args.trip_id,args.directory,
            lambda deadline=None:fast_ownership(k,pod['name'],pod['uid'],deadline))
        print(json.dumps({'samplingReady':result['samplingReady'],'injected':False,'gpuChanged':False}));return
    if args.action=='daemon-apply':
        plan=load(args.plan);hard.verify_daemon(plan,args.approve_daemon_sha256,True)
        result=hard.apply_daemon(f.Kube(args.kubeconfig),plan,args.approve_daemon_sha256,args.receipt)
        print(json.dumps({'daemonRetargeted':result['complete'],'injected':False,'gpuChanged':False}));return
    if args.action=='profile-apply':
        plan=load(args.plan);chaos.verify(plan,args.approve_profile_sha256)
        receipt=chaos.apply(f.Kube(args.kubeconfig),plan,args.approve_profile_sha256,args.receipt)
        print(json.dumps({'profileApplied':receipt['complete'],'injected':False,'gpuChanged':False}));return
    if args.action=='run':
        plan=load(args.plan);w.verify(plan,args.approve_plan_sha256)
        container=plan['case']=='W1K'
        f.require(container==args.ack_maintenance_and_one_container_kill,'Use acknowledgement for the exact fault mode')
        f.require((args.daemon_receipt is not None)==container,'Container kill needs its daemon receipt; PodKill must not move a daemon')
        dr=load(args.daemon_receipt)if container else None
        if container:
            f.require(dr.get('complete') and dr['plan']==plan['daemonPlan'],'Applied daemon receipt mismatch')
            f.require(args.sampling_proof is not None,'Run read-only sampling-probe before GPU start')
        else:f.require(args.sampling_proof is None,'Sampling proof belongs to W1K only')
        stop_guard=guard_ready(args.gpu_guard,plan)
        f.private_write(args.plan.parent/('.w1-'+plan['planSha256']+'-started.json'),{'planSha256':plan['planSha256'],'at':f.now(),'oneShotNeverRetry':True})
        try:
            if container:w.verify_sampling(load(args.sampling_proof),plan['snapshot'])
            rr=load(args.reader_receipt);pod=next(x for x in rr['resources'] if x['kind']=='Pod')
            if container:
                # Node-clock age, not Mac-vs-server age. Preserve 900s observation plus cleanup margin.
                kp=f.Kube(args.kubeconfig);rp=kp.get('pod','keda',pod['name']);own=fast_ownership(kp,pod['name'],pod['uid'])
                f.require(0<=own['clock']['nodeUnixSeconds']-w.epoch(rp['metadata']['creationTimestamp'])<=90,
                          'Reader too old for the full 900s trial; recreate before input')
        except BaseException:
            stop_guard() # GPU need not wait for watchdog expiry on a preflight failure.
            raise
        receipt=w.execute(f.Kube(args.kubeconfig),plan,args.approve_plan_sha256,args.directory,
            lambda deadline=None:(fast_ownership if container else ownership)(f.Kube(args.kubeconfig),pod['name'],pod['uid'],deadline),load(args.profile_receipt),stop_guard,
            lambda:reader_stop(f.Kube(args.kubeconfig),rr),dr)
        print(json.dumps({'oneShotRequested':receipt['injectionRequested'],'automaticRecovery':'use assess, not the controller status'}));return
    if args.action=='reader-start':
        result=reader_start(f.Kube(args.kubeconfig),load(args.bundle),args.approve_bundle_sha256,args.directory)
        print(json.dumps({'readerStarted':True,'name':result['name'],'gpuChanged':False}));return
    if args.action=='collect':
        observer=w.WorkerDbObserver(f.Kube(args.kubeconfig),load(args.snapshot),args.trip_id,args.directory)
        print(json.dumps({'sample':observer.sample()['observedAt'],'injected':False}));return
    if args.action=='snapshot':result=w.snapshot(f.Kube(args.kubeconfig))
    elif args.action=='profile-plan':result=chaos.profile_plan(f.Kube(args.kubeconfig))
    elif args.action=='reader-stop':result=reader_stop(f.Kube(args.kubeconfig),load(args.receipt))
    elif args.action=='profile-restore':result=chaos.restore(f.Kube(args.kubeconfig),load(args.receipt))
    elif args.action=='daemon-plan':result=hard.daemon_plan(f.Kube(args.kubeconfig),load(args.snapshot))
    elif args.action=='daemon-restore':result=hard.restore_daemon(f.Kube(args.kubeconfig),load(args.receipt))
    elif args.action=='plan':
        f.require((args.failure_mode=='container-kill')==(args.daemon_plan is not None),'Container-kill requires a daemon Plan')
        result=w.prepare(load(args.snapshot),args.trip_id,args.run_id,load(args.input),args.ack_maintenance,load(args.daemon_plan)if args.daemon_plan else None)
    elif args.action=='reader-bundle':result=reader_bundle(args.kafka_client,args.node,args.name)
    elif args.action=='summarize':result=w.summarize(args.directory)
    elif args.action=='dashboard':result=w.dashboard(load(args.record))
    else:result=w.assess(load(args.before),load(args.after),load(args.timing),load(args.trace),load(args.db_summary))
    f.private_write(args.output,result)
    print(json.dumps({'prepared':True,'injected':False,'planSha256':result.get('planSha256')}))


if __name__=='__main__':
    try:main()
    except (ValueError,TypeError,KeyError,OSError,subprocess.SubprocessError):
        print(json.dumps({'status':'blocked','reason':'Inspect prerequisites and private receipts; raw data withheld'}),file=sys.stderr);sys.exit(1)
