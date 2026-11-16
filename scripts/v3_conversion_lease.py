"""Opt-in W2K lease comparison. Frozen W1/W1K criteria and results remain unchanged.

No AWS API, upload, DB write, image pull or automatic policy activation.
Image inspection is structural compatibility evidence, not proof of runtime safety.
"""
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile
import v3_cdc_fault as f

ENV = ('ATTACHMENT_CONVERSION_LEASE_DURATION', 'ATTACHMENT_CONVERSION_LEASE_RENEW_INTERVAL')
IMAGE = re.compile(r'ghcr.io/100-hours-a-week/yeodam-backend@sha256:[a-f0-9]{64}')
SHA = re.compile(r'[a-f0-9]{64}')
SOURCES = re.compile(r'[a-f0-9]{40}')
COHORT_RULE = 'first-post-exit137-read/all-started-unsaved-items-in-preselected-task/v1'
QUERIES = {
 'lease_seconds': 'yeodam_attachment_conversion_lease_duration_seconds{namespace="photo"}',
 'renew_interval_seconds': 'yeodam_attachment_conversion_lease_renew_interval_seconds{namespace="photo"}',
 'renew_gap_max': 'yeodam_attachment_conversion_lease_renew_gap_seconds_max{namespace="photo"}',
 'renew_duration_max': 'yeodam_attachment_conversion_lease_renew_duration_seconds_max{namespace="photo"}',
 'renew_outcomes': 'yeodam_attachment_conversion_lease_renew_total{namespace="photo"}',
 'heartbeat_gap_max': 'yeodam_attachment_conversion_lease_heartbeat_gap_seconds_max{namespace="photo"}',
 'heartbeat_delay_max': 'yeodam_attachment_conversion_lease_heartbeat_delay_seconds_max{namespace="photo"}',
 'heartbeat_renew_max': 'yeodam_attachment_conversion_lease_heartbeat_renew_seconds_max{namespace="photo"}',
 'heartbeat_outcomes': 'yeodam_attachment_conversion_lease_heartbeat_total{namespace="photo"}',
}


def policy(data):
    present = set(ENV) & set(data)
    if not present: return None
    f.require(present == set(ENV), 'Both lease settings required; no implicit mixed policy')
    seconds=[]
    for key in ENV:
        value=data[key]
        f.require(isinstance(value,str) and re.fullmatch(r'[1-9][0-9]{0,3}s',value), 'Explicit integer seconds required')
        seconds.append(int(value[:-1]))
    duration,renew=seconds
    f.require(renew == 30 and 90 <= duration <= 600 and duration >= 3*renew,
              'Reviewed 30s heartbeat and 90..600s lease only; vary one parameter')
    return {'durationSeconds':duration,'renewIntervalSeconds':renew}


def inspect_jar(path, source, image=None):
    path=Path(path)
    f.require(path.is_file() and not path.is_symlink() and path.stat().st_size<=1024*1024*1024,'Bounded regular app jar required')
    f.require(SOURCES.fullmatch(source or '') and (image is None or IMAGE.fullmatch(image)), 'Exact source and optional immutable image required')
    names={
      'policy':('trip/config/AttachmentConversionLeaseProperties.class',[b'app.attachment-conversion.lease',b'renewInterval',b'duration']),
      'transaction':('trip/service/AttachmentConversionTransactionService.class',[b'AttachmentConversionLeaseProperties',b'duration',b'plus',b'isClaimOwner']),
      'worker':('trip/service/AttachmentConversionWorker.class',[b'AttachmentConversionLeaseProperties',b'scheduleWithFixedDelay',b'toMillis',b'yeodam.attachment.conversion.lease.heartbeat.gap',b'yeodam.attachment.conversion.lease.heartbeat.delay']),
      'yaml':('../application.yaml', [x.encode()for x in ENV]),
    }
    hashes={}
    with zipfile.ZipFile(path)as jar:
        for key,(suffix,needles)in names.items():
            name='BOOT-INF/classes/application.yaml' if key=='yaml'else 'BOOT-INF/classes/com/yeodam/yeodambe/'+suffix
            f.require(name in jar.namelist() and jar.getinfo(name).file_size<=2*1024*1024,'Lease-aware image class/config missing')
            raw=jar.read(name)
            f.require(all(x in raw for x in needles),'Lease policy/scheduler/ownership instrumentation not supported')
            hashes[key]=hashlib.sha256(raw).hexdigest()
    value={'version':1,'mode':'image' if image else 'local-jar','beSource':source,'beImage':image,
           'structuralCompatibility':True,'filesSha256':hashes,'semanticSafetyProven':False}
    return dict(value,proofSha256=f.sha(value))


def inspect_image(image, source):
    f.require(IMAGE.fullmatch(image or ''),'Immutable reviewed BE image required')
    subprocess.run(['docker','image','inspect',image],check=True,capture_output=True,timeout=15)
    cid=subprocess.check_output(['docker','create','--network','none','--entrypoint','/bin/true',image],text=True,timeout=20).strip()
    f.require(re.fullmatch('[a-f0-9]{64}',cid),'Unexpected local container ID')
    try:
        with tempfile.TemporaryDirectory(prefix='v3-lease-image-')as tmp:
            path=Path(tmp)/'app.jar'
            subprocess.run(['docker','cp',cid+':/app/app.jar',str(path)],check=True,capture_output=True,timeout=60)
            return inspect_jar(path,source,image)
    finally:
        subprocess.run(['docker','rm',cid],check=True,capture_output=True,timeout=20)


def validate_support(s, component):
    f.require(s.get('proofSha256')==f.sha({k:v for k,v in s.items()if k!='proofSha256'}) and
        s.get('version')==1 and s.get('mode')=='image' and s.get('structuralCompatibility') is True and
        s.get('semanticSafetyProven') is False and s.get('beSource')==component['sourceSha'] and
        s.get('beImage')==component['image'] and set(s.get('filesSha256',{}))=={'policy','transaction','worker','yaml'} and
        all(SHA.fullmatch(x)for x in s['filesSha256'].values()),'Exact inspected image proof required; local jar is not deployment proof')


def budget_ok(b, duration):
    keys=['maxObservedRenewGapSeconds','maxObservedRenewSeconds','unobservedPauseAllowanceSeconds']
    f.require(isinstance(b,dict) and all(type(b.get(k))in(int,float) and math.isfinite(b[k]) and b[k]>=0 for k in keys),'Finite measured renewal budget required')
    f.require(b[keys[0]]>0 and len(b.get('rationale','').strip())>=20 and b.get('evidenceSha256') and
              all(SHA.fullmatch(x) for x in b['evidenceSha256']), 'Record measured gap, evidence and pause-margin rationale')
    # Explicit engineering margin: twice the observed gap+renew cost plus a separately justified unobserved pause.
    floor=max(90,2*(max(30,b[keys[0]])+b[keys[1]])+b[keys[2]])
    f.require(duration>floor,'Proposed lease does not exceed the measured/margin budget')
    return floor



def measured_budget(metrics, allowance, rationale):
    f.require(type(allowance)in(int,float) and math.isfinite(allowance) and allowance>=0 and len(rationale.strip())>=20,'Explicit finite pause allowance and rationale required')
    vals=[]
    for key in ['renew_gap_max','renew_duration_max']:
        row=metrics.get('ranges',{}).get('conversion_'+key,{})
        points=[v for series in row.get('series',[])for _,v in series.get('values',[])]
        f.require(row.get('status')=='ok' and points and all(type(v)in(int,float) and math.isfinite(v) and v>=0 for v in points) and max(points)>0,'Actual positive renewal observations required; missing/zero cannot establish a budget')
        vals.append(max(points))
    f.require(metrics.get('leaseReviewSha256') and metrics.get('leaseVictimPod'),'Budget must use exact reviewed Pod observations')
    return {'maxObservedRenewGapSeconds':vals[0],'maxObservedRenewSeconds':vals[1],
            'unobservedPauseAllowanceSeconds':allowance,'rationale':rationale,'evidenceSha256':[f.sha(metrics)],
            'window':{'start':metrics['start'],'end':metrics['end']},'rollingMaximaNotStatisticalSlo':True}

def candidate(source,support,duration,budget=None):
    import v3_release_bundle as bundle
    bundle.prepared(source);validate_support(support,source['components']['backend'])
    f.require(type(duration)is int,'Integer lease duration required')
    target=dict(zip(ENV,[str(duration)+'s','30s']));policy(target)
    if duration<600:budget_ok(budget,duration)
    result=copy.deepcopy(source);be=result['values']['backend']
    for name,workload in be['workloads'].items():
        data=bundle.apps.merge(be['defaults'],workload).get('configMapData',{})
        f.require(name=='photo-converter' or not(set(ENV)&set(data)),'Lease settings may only belong to converter')
    data=bundle.apps.merge(be['defaults'],be['workloads']['photo-converter'])['configMapData'];policy(data)
    be['workloads']['photo-converter'].setdefault('configMapData',{}).update(target)
    bundle.prepared(result)
    return result


def review(source,support,approved_revision,budget=None):
    import v3_release_bundle as bundle
    bundle.prepared(source);validate_support(support,source['components']['backend'])
    f.require(SOURCES.fullmatch(approved_revision or ''),'Exact approved Snapshot revision required')
    data=bundle.apps.merge(source['values']['backend']['defaults'],source['values']['backend']['workloads']['photo-converter'])['configMapData']
    pol=policy(data);f.require(pol is not None,'Explicit converter policy required')
    if pol['durationSeconds']<600:budget_ok(budget,pol['durationSeconds'])
    # Bind rendered/runtime identity; changes to images, CA references, limits or scaling are compared separately.
    normalized=copy.deepcopy(source)
    data=normalized['values']['backend']['workloads']['photo-converter'].setdefault('configMapData',{})
    for key in ENV:data.pop(key,None)
    value={'version':1,'nonPolicyCandidateSha256':f.sha(normalized),'policy':pol,'beSource':support['beSource'],'beImage':support['beImage'],
           'approvedRevision':approved_revision,'candidateSha256':f.sha(source),'support':support,'budget':budget,
           'cohortRule':COHORT_RULE,'case':'W2K','baselineMustUseSameImage':True}
    return dict(value,reviewSha256=f.sha(value))


def validate_review(r):
    f.require(r.get('reviewSha256')==f.sha({k:v for k,v in r.items()if k!='reviewSha256'}) and
        r.get('version')==1 and r.get('case')=='W2K' and r.get('cohortRule')==COHORT_RULE and
        r.get('baselineMustUseSameImage') is True and SOURCES.fullmatch(r.get('approvedRevision','')) and
        SHA.fullmatch(r.get('candidateSha256','')) and SHA.fullmatch(r.get('nonPolicyCandidateSha256','')) and SOURCES.fullmatch(r.get('beSource','')) and IMAGE.fullmatch(r.get('beImage','')),'Exact W2K review required')
    pol=r['policy'];f.require(pol==policy(dict(zip(ENV,[str(pol['durationSeconds'])+'s',str(pol['renewIntervalSeconds'])+'s']))),'Policy mismatch')
    validate_support(r['support'],{'sourceSha':r['beSource'],'image':r['beImage']})
    if pol['durationSeconds']<600:budget_ok(r.get('budget'),pol['durationSeconds'])
    return r


def criteria(r):
    validate_review(r)
    import v3_worker_fault as w
    value=w.criteria(True)
    value.update(leaseSeconds=r['policy']['durationSeconds'],heartbeatSeconds=30,postDeathCohortRule=COHORT_RULE,
                 terminationToCompletionTargetSeconds=max(r['policy']['durationSeconds'],180)+15+240+30)
    value['basis']=dict(value['basis'],overall='max(policy lease, earlier cold readiness rounded to 180) + claim 15 + completion 240 + observer/variation 30; engineering target, not SLO',
                         diagnostic='900s observation unchanged for both policies; never shorten observation to censor failures')
    return value


def cohort(selected,proof):
    import v3_worker_fault as w
    w.validate_rows(proof['rows'],proof['rows'][0]['tripId'],True)
    task=next((x for x in proof['rows']if x['section']=='worker_task'and x['taskId']==selected['taskId']),None)
    items=[x for x in proof['rows']if x['section']=='worker_item'and x['batchId']==selected['batchId']]
    members=[{'itemId':x['itemId'],'attempt':x['attempt']}for x in items if x.get('startedAt') is not None and not x['saved'] and x['attempt']>0]
    saved=[{'itemId':x['itemId'],'attempt':x['attempt'],'derivedHash':x['derivedHash'],'attachmentId':x['attachmentId']}for x in items if x['saved']]
    owner=bool(task and task['status']=='RUNNING' and task['claimHash']==selected['claimHash'] and
               type(task.get('leaseSeconds'))in(int,float) and math.isfinite(task['leaseSeconds']) and task['leaseSeconds']>0)
    matched=bool(owner and len(members)==1 and task.get('attemptOwnerHash')==selected['claimHash'] and
                 members[0]=={'itemId':task.get('currentItem'),'attempt':task.get('currentAttempt')})
    # An expected post-kill race is a non-qualified trial, not a reason to stop business recovery after injection.
    return {'rule':COHORT_RULE,'taskId':selected['taskId'],'batchId':selected['batchId'],'claimHash':selected['claimHash'],
            'proofSha256':f.sha(proof),'observedAt':proof['observedAt'],'members':members,'saved':saved,
            'oldOwnerStillValid':owner,'currentAttemptOwnershipMatched':matched,
            'qualified':matched,'notLegacyW1KPass':True}



def metrics_conditions(m):
    import v3_scaling_evidence as scaling
    a,b=m['conditionsBefore'],m['conditionsAfter']
    f.require(scaling.compare_conditions(a,b)['staticConditionsMatch'] is True,'Runtime image/resources/scaling changed during measurement')
    apps=copy.deepcopy(a['apps'])
    for app in apps:
        app.pop('replicas',None)
        if app.get('namespace')=='photo' and app.get('name')=='photo-converter':app.pop('podTemplateSha256',None)
    # The converter template checksum legitimately changes with the two policy keys;
    # the normalized full candidate separately proves every other config value is identical.
    return f.sha({'apps':apps,'scalingPolicies':a['scalingPolicies'],'nodeTypes':a['nodeTypes']})


def metrics_ready(m,plan,timing):
    import v3_worker_fault as w
    f.require(m.get('leaseReviewSha256')==plan['snapshot']['leaseReview']['reviewSha256'] and
              m.get('leaseVictimPod')==plan['snapshot']['victim']['name'],'Metrics must pin this exact review/Pod')
    f.require(m['conditionsBefore']['approvedRevision']==m['conditionsAfter']['approvedRevision']==plan['snapshot']['approvedRevision'],'Metrics must bind this approved deployment')
    f.require(w.epoch(m['start'])<=w.epoch(timing['terminationLowerAt']) and
              w.epoch(m['end'])>=math.floor(w.epoch(timing['completedAtBounds'][1])),'Metrics window does not cover kill through completion')
    conditions=metrics_conditions(m)
    rows=m.get('ranges',{})
    ok=all(rows.get('conversion_'+key,{}).get('status')=='ok' and rows['conversion_'+key].get('series') for key in QUERIES)
    for key in QUERIES:
        points=[v for series in rows.get('conversion_'+key,{}).get('series',[])for _,v in series.get('values',[])]
        ok=ok and bool(points) and all(type(v)in(int,float) and math.isfinite(v) and v>=0 for v in points)
    for key,expected in [('lease_seconds',plan['criteria']['leaseSeconds']),('renew_interval_seconds',30)]:
        points=[v for series in rows.get('conversion_'+key,{}).get('series',[])for _,v in series.get('values',[])]
        ok=ok and bool(points) and all(type(v)in(int,float) and math.isfinite(v) and v==expected for v in points)
    return bool(ok),conditions

def assess(plan,directory,trace,metrics):
    import v3_worker_fault as w
    d=Path(directory);w.verify(plan,plan['planSha256'],fresh=False)
    f.require(plan['case']=='W2K','Preregistered W2K Plan required; never regrade W1K')
    load=lambda name:json.loads((d/name).read_text())
    timing=w.summarize(d);before=load('pre-injection.json');after=load('final.json');db=load('db-summary.json');receipt=load('receipt.json')
    original=w.assess(before,after,dict(timing,case='W1K'),trace,db)
    proof=load('post-termination.json');frozen=load('post-death-cohort.json');selected=load('partial.json')
    f.require(frozen==cohort(selected,proof) and frozen['proofSha256']==f.sha(proof),'Frozen first post-death cohort must match original evidence')
    ends=[json.loads(x.read_text())for x in sorted(d.glob('runtime-*.json'))]
    ends=[x for x in ends if (x.get('processTermination')or{}).get('killedContainerId')==plan['snapshot']['victim']['containerId'] and x['processTermination']['exitCode']==137]
    f.require(ends and proof.get('queryStartedAt') and w.epoch(proof['queryStartedAt'])>=w.epoch(ends[0]['at']),'Cohort DB read must follow observed old-container exit')
    items={x['itemId']:x for x in after['rows']if x['section']=='worker_item'}
    retried=frozen['qualified'] and all(x['itemId']in items and items[x['itemId']]['saved']==1 and items[x['itemId']]['attempt']>x['attempt']for x in frozen['members'])
    preserved=all(x['itemId']in items and all(items[x['itemId']][k]==x[k]for k in ['attempt','derivedHash','attachmentId'])for x in frozen['saved'])
    killed=timing.get('processKillObserved')is True and receipt.get('chaosInjectedObserved')is True
    known=all(timing.get(k)is not None for k in ['terminationLowerAt','terminationUpperAt','completedAtBounds','eligibilityToClaimBounds','claimToCompletionBounds'])
    metric_ready,condition_fingerprint=metrics_ready(metrics,plan,timing)
    status=next(x['status']for x in after['rows']if x['section']=='execution_timing')
    recovery=bool(status=='COMPLETED' and metric_ready and killed and retried and preserved and original['identityMatched'] and original['savedItemsPreserved'] and original['traceAvailable'] and known and
        timing.get('prematureClaimObserved')is False and not timing.get('leaseReleaseObserved') and
        db.get('continuousObservationComplete')is True and not db.get('observationGaps') and not timing.get('manualActions'))
    total=None
    if known:
        done=[w.epoch(x)for x in timing['completedAtBounds']]
        total=[max(0,done[0]-w.epoch(timing['terminationUpperAt'])),max(0,done[1]-w.epoch(timing['terminationLowerAt']))]
    return {'case':'W2K','planSha256':plan['planSha256'],'reviewSha256':plan['snapshot']['leaseReview']['reviewSha256'],
        'nonPolicyCandidateSha256':plan['snapshot']['leaseReview']['nonPolicyCandidateSha256'],
        'policy':plan['snapshot']['leaseReview']['policy'],'beImage':plan['snapshot']['beImage'],'input':plan['input'],
        'conditionsFingerprint':condition_fingerprint,'metricsSourceSha256':f.sha(metrics),'leaseMetricsReady':metric_ready,
        'evidenceComplete':recovery,'automaticRecovery':True if recovery else None,'cohortRetried':bool(retried),
        'savedResultsPreserved':preserved,'finalIdentityMatched':original['identityMatched'],'traceReady':original['traceAvailable'],
        'eligibilityToClaimBounds':timing.get('eligibilityToClaimBounds'),'claimToCompletionBounds':timing.get('claimToCompletionBounds'),
        'terminationToCompletionBounds':total,'overallTargetSeconds':plan['criteria']['terminationToCompletionTargetSeconds'],
        'overallTargetMet':w.bounded_target(total,plan['criteria']['terminationToCompletionTargetSeconds']),
        'cohort':frozen,'legacyAssessment':original,'timing':timing,'exactlyOnceProven':False,'allS3SideEffectsVerified':False}


def compare(before,after,conditions_equal):
    f.require(before.get('case')==after.get('case')=='W2K','Legacy exploratory result is context, not a causal baseline')
    f.require(before.get('evidenceComplete')is True and after.get('evidenceComplete')is True,'Two complete preregistered outcomes required')
    f.require(before.get('conditionsFingerprint')==after.get('conditionsFingerprint') and SHA.fullmatch(before.get('conditionsFingerprint','')) and before['beImage']==after['beImage'] and before['input']==after['input'] and
              before.get('nonPolicyCandidateSha256')==after.get('nonPolicyCandidateSha256') and SHA.fullmatch(before.get('nonPolicyCandidateSha256','')) and conditions_equal is True,
              'Same supported image, input and reviewed non-policy conditions required')
    f.require(before['policy']=={'durationSeconds':600,'renewIntervalSeconds':30} and after['policy']['renewIntervalSeconds']==30 and
              after['policy']['durationSeconds']<600,'Vary lease duration only')
    a=before['terminationToCompletionBounds'];b=after['terminationToCompletionBounds']
    f.require(all(isinstance(x,list) and len(x)==2 and all(type(v)in(int,float) and math.isfinite(v)for v in x) and 0<=x[0]<=x[1] for x in [a,b]),'Finite ordered observation bounds required')
    reduction=[a[0]-b[1],a[1]-b[0]]
    return {'reductionSecondsBounds':reduction,'improvementEstablished':reduction[0]>0,'statisticalSlo':False,
            'singleSampleComparison':True,'confounders':'GPU/model variability and observer uncertainty remain; not a general performance guarantee'}
