"""CDC C0/C1/C2: offline, hash-bound plans; no GPU/offset/Task lifecycle API.

Time targets are experimental decisions, never provider guarantees. A controller
AllRecovered timestamp is NOT the instant a TCP stream or delivery recovered.
"""
import json
import hashlib
import ipaddress
import re
from datetime import datetime, timezone
import math
from pathlib import Path
import time
import urllib.request
import urllib.parse
import v3_cdc_fault as f

KAFKA = 'yeodam-v3-kafka'
# Source-derived, NOT a readout from the operating JVM. Validate overrides below.
WORKER_TIMERS = {'session.timeout.ms': 10000, 'heartbeat.interval.ms': 3000,
                 'rebalance.timeout.ms': 60000, 'worker.sync.timeout.ms': 3000,
                 'worker.unsync.backoff.ms': 300000, 'scheduled.rebalance.max.delay.ms': 300000, 'offset.flush.interval.ms': 60000,
                 'offset.flush.timeout.ms': 5000, 'producer.request.timeout.ms': 30000,
                 'producer.delivery.timeout.ms': 2147483647,
                 'producer.max.block.ms': 9223372036854775807}


def kafka_snapshot(k, controller_node=None, allow_worker=False):
    s = f.snapshot(k, controller_node, allow_worker)
    pods = k.get('pods', 'kafka', selector='strimzi.io/name='+KAFKA+'-kafka')['items']
    f.require(len(pods) == 3, 'Exactly three reviewed brokers required')
    records = []
    for p in pods:
        f.require(p['metadata']['labels'].get('strimzi.io/pool-name') == 'dual-role', 'Wrong broker pool')
        records.append(f.pod_record(p))
    svc = k.get('service', 'kafka', KAFKA+'-kafka-bootstrap')
    ports = [p for p in svc['spec']['ports'] if p['port'] == 9093]
    f.require(len(ports) == 1 and ports[0]['protocol'] == 'TCP' and ports[0]['targetPort'] == 9093,
              'Unexpected Kafka TLS port mapping')
    slices = k.get('endpointslices', 'kafka', selector='kubernetes.io/service-name='+KAFKA+'-kafka-bootstrap')['items']
    f.require(all(any(p.get('port') == 9093 and p.get('protocol') == 'TCP' for p in x['ports']) for x in slices),
              'Kafka endpoint port mismatch')
    endpoints = [e for x in slices for e in x.get('endpoints', [])]
    expected = {(p['uid'], p['ip']) for p in records}
    actual = {(e.get('targetRef', {}).get('uid'), a) for e in endpoints
              if e.get('conditions', {}).get('ready') is True and
              e.get('targetRef', {}).get('kind') == 'Pod' for a in e.get('addresses', [])}
    f.require(len(endpoints) == 3 and expected == actual, 'Broker endpoints/UIDs mismatch')
    connect = k.get('kafkaconnect', 'kafka', f.CONNECT)
    cfg = connect['spec']['config']
    f.require(connect['spec']['version'] == '4.2.2' and cfg.get('connector.client.config.override.policy') == 'None',
              'Review changed Kafka version or client override policy')
    # Inspect numeric allowlist only; never persist SASL/config providers/Secret values.
    timers = {key: cfg.get(key, value) for key, value in WORKER_TIMERS.items()}
    f.require(all(str(timers[key]) == str(value) for key, value in WORKER_TIMERS.items()),
              'Review changed retry/group timers before a Kafka fault')
    connector = k.get('kafkaconnector', 'kafka', f.CONNECTOR)
    f.require(not any(x.startswith('producer.override.') for x in connector['spec']['config']),
              'Unexpected connector client overrides')
    s['kafka'] = {'brokers': sorted(records, key=lambda p: p['name']), 'serviceIp': svc['spec']['clusterIP'],
                  'serviceUid': svc['metadata']['uid'], 'port': 9093,
                  'image': connect['spec']['image'], 'timers': timers,
                  'configSha256': f.sha(cfg), 'connectorConfigSha256': f.sha(connector['spec']['config']),
                  'timerEvidence': 'Kafka 4.2.2 source defaults plus Kubernetes CR numeric overrides; not JVM dump'}
    kafka_targets(s)
    return s


def kafka_targets(s):
    v = s.get('kafka', {})
    f.require(v.get('port') == 9093 and len(v.get('brokers', [])) == 3, 'Reviewed Kafka snapshot required')
    f.require(f.UUID.fullmatch(v.get('serviceUid', '')) and
              re.fullmatch(r'ghcr.io/100-hours-a-week/yeodam-v3-connect@sha256:[a-f0-9]{64}', v.get('image', '')), 'Kafka service/image identity required')
    f.require(len({p['uid'] for p in v['brokers']}) == len({p['ip'] for p in v['brokers']}) == 3,
              'Duplicate Kafka broker identity')
    for p in v['brokers']:
        f.require(p['ownerKind'] == 'StrimziPodSet' and p['ownerName'] == KAFKA+'-dual-role' and
                  p['name'].startswith(KAFKA+'-dual-role-') and f.UUID.fullmatch(p['uid']) and
                  ipaddress.ip_address(p['ip']) in ipaddress.ip_network('10.40.0.0/16'), 'Wrong Kafka broker identity')
    f.require(ipaddress.ip_address(v['serviceIp']) in ipaddress.ip_network('172.20.0.0/16'), 'Wrong Kafka service IP')
    f.require(set(v['timers']) == set(WORKER_TIMERS) and all(str(v['timers'][k]) == str(x) for k,x in WORKER_TIMERS.items()),
              'Unreviewed Kafka timer profile')
    return sorted({p['ip']+':9093' for p in v['brokers']} | {v['serviceIp']+':9093'})


def criteria(case):
    f.require(case in ('C0', 'C1', 'C2'), 'Unknown case')
    common = {'detectSeconds': 60, 'detectFrom': 'first active trial Outbox created_at',
              'detectTo': 'first observed Prometheus firing evaluation',
              'detectBudget': {'age': 15, 'sqlSleep': 5, 'scrape': 15, 'evaluation': 15, 'margin': 10},
              'baselineMaxSeconds': 200.206262, 'baselineSamples': 3,
              'guaranteed': False, 'statisticalSlo': False}
    if case != 'C2':
        common.update(deliverySeconds=30, completionSeconds=270, observationSeconds=360,
                      basis={'delivery': 'keepalive 10s; prior single first-publication 10.492s; 30s experimental target',
                             'completion': 'ceil baseline max to 210 + delivery 30 + variation allowance 30',
                             'observation': '270 target + 90 diagnostic allowance; manual action reported separately'})
    else:
        # Never borrow the MySQL reconnect target. Allow one unsync backoff cycle
        # plus group join/metadata acquisition/offset flush budgets; not all paths use all timers.
        common.update(deliverySeconds=60, completionSeconds=300, observationSeconds=780,
                      basis={'delivery': 'two request-timeout 30s windows chosen as initial operational target; no C2 baseline yet, not a Kafka recovery bound',
                             'completion': 'delivery target 60 + rounded normal baseline 210 + variability allowance 30 = 300',
                             'observation': '300 unsync + 300 scheduled rebalance + 60 join + 30 request + 60 offset interval + 30 margin = 780 diagnostic policy; paths may overlap, not a guaranteed or mandatory sequence',
                             'scope': 'all Connect Kafka TLS traffic including config/status/offset/history; at-least-once replay possible'})
    return common


def prepare(s, run_id, case, c0=None):
    f.require(case in ('C0', 'C1', 'C2'), 'Unknown case')
    phases = [90]
    gap = 0
    if case == 'C1':
        # C0 result is evidence, not a user-provided guessed number.
        f.require(isinstance(c0, dict) and c0.get('case') == 'C0' and c0.get('automaticRecovery') is True and
                  c0.get('approvedRevision') == s['approvedRevision'] and c0.get('evidenceComplete') is True and c0.get('criteria') == criteria('C0') and
                  set(c0.get('sourceEvidenceHashes',{})) == {'accepted','final','trace','timing'},
                  'C1 requires a complete current-deployment C0 result')
        delivery = c0.get('releaseToDeliverySeconds')
        f.require(type(delivery) in (int,float) and math.isfinite(delivery) and 0 < delivery <= 30,
                  'C0 delivery target not met; review C1 gap instead of silently extending it')
        gap = max(30, math.ceil(2*delivery/10)*10)
        phases = [45, 45]
    plans = [f.prepare(s, run_id+'-'+str(i+1), seconds, 'kafka' if case=='C2' else 'mysql') for i,seconds in enumerate(phases)]
    value = {'version': 1, 'case': case, 'plans': plans, 'recoveryWindowSeconds': gap,
             'criteria': criteria(case), 'c0EvidenceSha256': f.sha(c0) if c0 else None,
             'faultModuleSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'installed': False, 'injected': False}
    return dict(value, planSha256=f.sha(value))


def verify(p, expected):
    f.require(p['planSha256'] == expected == f.sha({k:v for k,v in p.items() if k!='planSha256'}), 'Followup plan hash mismatch')
    f.require(p['faultModuleSha256'] == hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'Followup tool changed')
    f.require(p['criteria'] == criteria(p['case']), 'Changed criteria; prepare/review a new plan')
    f.require(len(p['plans']) == (2 if p['case']=='C1' else 1), 'Wrong phase count')
    f.require(p['recoveryWindowSeconds'] in (30,40,50,60) if p['case']=='C1' else p['recoveryWindowSeconds']==0, 'Wrong recovery window')
    for x in p['plans']:
        f.verify_plan(x, x['planSha256'])
        f.require(x['seconds'] == (45 if p['case']=='C1' else 90) and
                  x.get('destination','mysql') == ('kafka' if p['case']=='C2' else 'mysql'), 'Changed phase')
    f.require(all(x['snapshot']==p['plans'][0]['snapshot'] for x in p['plans']), 'Mixed snapshots')


class Observer:
    """Read a single localhost Prometheus. Each poll preserves scrape/evaluation axes.

    No arbitrary queries or redirects. Missing/old metrics cannot authorize a fault.
    Raw scrape timestamp is distinct from this client's wall clock.
    """
    def __init__(self, base, directory):
        u=urllib.parse.urlsplit(base)
        f.require(u.scheme=='http' and u.hostname in ('127.0.0.1','localhost') and u.port and
                  u.path in ('','/') and not any((u.username,u.password,u.query,u.fragment)), 'Use private localhost Prometheus tunnel')
        self.base=base.rstrip('/'); self.directory=Path(directory); self.count=0; self.last_poll=None; self.last_result=False; self.initial_clear=False
    def get(self,path):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs): return None
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        with opener.open(self.base+path,timeout=4) as response:
            raw=response.read(512*1024+1)
        f.require(len(raw)<=512*1024,'Observer response too large')
        return f.bounded_json(raw.decode())
    def __call__(self):
        if self.last_poll is not None and time.monotonic()-self.last_poll<5: return self.last_result
        self.last_poll=time.monotonic()
        self.count+=1
        record={'observedAt':f.now(),'healthy':False}
        try:
            expr='{__name__=~"yeodam_outbox_(probe_success|last_success_timestamp_seconds|query_started_timestamp_seconds|query_completed_timestamp_seconds|query_duration_seconds)|up",job="cdc-status"}'
            raw=self.get('/api/v1/query?'+urllib.parse.urlencode({'query':expr}))
            f.require(raw.get('status')=='success','Prometheus query failed')
            samples=raw['data']['result']; record['samples']=samples
            by={x['metric']['__name__']:x for x in samples}
            required=('up','yeodam_outbox_probe_success','yeodam_outbox_query_started_timestamp_seconds',
                      'yeodam_outbox_query_completed_timestamp_seconds','yeodam_outbox_query_duration_seconds')
            f.require(all(sum(x['metric']['__name__']==n for x in samples)==1 for n in required),'Missing/ambiguous timing metrics')
            rules=self.get('/api/v1/rules?type=alert')
            found=[dict(rule) for g in rules.get('data',{}).get('groups',[]) for rule in g['rules']
                   if rule.get('name')=='V3ConversionDeliveryStalled']
            f.require(rules.get('status')=='success' and len(found)==1 and found[0].get('health')=='ok', 'Alert evaluation unavailable')
            rule=found[0]
            record['rule']={k:rule.get(k) for k in ('state','lastEvaluation','evaluationTime','alerts')}
            stamps=self.get('/api/v1/query?'+urllib.parse.urlencode({'query':'timestamp(yeodam_outbox_last_success_timestamp_seconds{job="cdc-status"})'}))
            f.require(stamps.get('status')=='success' and len(stamps['data']['result'])==1,'Missing scrape timestamp')
            scrape=float(stamps['data']['result'][0]['value'][1]);record['scrapeTimestamp']=scrape
            # Read the server clock AFTER the observations. Instant-query timestamps
            # and rule evaluation timestamps belong to Prometheus, not this Mac.
            local_start=time.time();mono_start=time.monotonic()
            clock=self.get('/api/v1/query?'+urllib.parse.urlencode({'query':'vector(time())'}))
            local_end=time.time();elapsed=time.monotonic()-mono_start
            f.require(clock.get('status')=='success' and len(clock['data']['result'])==1,'Server clock unavailable')
            epoch=float(clock['data']['result'][0]['value'][1])
            f.require(math.isfinite(epoch) and epoch>0 and 0<=elapsed<=4 and local_end>=local_start,'Invalid clock observation')
            record['clock']={'prometheusEpochSeconds':epoch,'roundTripSeconds':elapsed,
                             'serverMinusLocalIntervalSeconds':[epoch-local_end,epoch-local_start]}
            f.require(all(0<=epoch-float(by[n]['value'][0])<=25 for n in required),'Old Prometheus evaluation')
            f.require(all(float(by[n]['value'][1])==1 for n in ('up','yeodam_outbox_probe_success')) and
                      0<=epoch-float(by['yeodam_outbox_query_completed_timestamp_seconds']['value'][1])<=25,
                      'Outbox scrape/query not fresh')
            stamp=datetime.fromisoformat(rule['lastEvaluation'].replace('Z','+00:00')).timestamp()
            f.require(0<=epoch-stamp<=30,'Alert evaluation stale')
            f.require(0<=epoch-scrape<=25,'Scrape stale')
            if not self.initial_clear:
                f.require(rule.get('state') == 'inactive', 'Existing delivery alert prevents a fresh detection measurement')
            record['healthy']=True
            self.initial_clear=True
        except (OSError,ValueError,KeyError,TypeError):
            record['error']='observation_unavailable'
        f.private_write(self.directory/f'observation-{self.count:05d}.json',record)
        self.last_result=record['healthy']
        return record['healthy']


def delete_recovered(k,p,uid):
    obj=k.get('networkchaos','kafka',p['runId']);f.owned_fault(obj,p,uid)
    f.require(obj['metadata']['annotations'].get(f.PAUSE)=='true' and any(
        c.get('type')=='AllRecovered' and c.get('status')=='True' for c in obj.get('status',{}).get('conditions',[])), 'Do not delete unrecovered fault')
    # API precondition prevents deletion of a same-name replacement.
    body={'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':uid,'resourceVersion':obj['metadata']['resourceVersion']}}
    k.call(['delete','--raw','/apis/chaos-mesh.org/v1alpha1/namespaces/kafka/networkchaos/'+p['runId'],'-f','-'],json.dumps(body))


def run(k,p,expected,directory,observe,trial_ids):
    verify(p,expected); directory=Path(directory)
    f.require(1<=len(trial_ids)<=1,'One pre-created trial trip per case'); f.trip_ids(trial_ids)
    receipts=[]; gap_record=None
    # Fresh observer required before ALL phases. Interrupt recovery is in execute finally.
    for i,phase in enumerate(p['plans']):
        f.require(observe() is True,'Observation unavailable; no next injection')
        if i:
            elapsed=(datetime.now(timezone.utc)-datetime.fromisoformat(receipts[-1]['recoveryRequestedAt'])).total_seconds()
            started=time.monotonic()
            while time.monotonic()-started<max(0,p['recoveryWindowSeconds']-elapsed):
                f.require(observe() is True,'Observation failed during recovered window; no reinjection')
                time.sleep(5)
            after=f.collect_db(k,trial_ids)
            release=datetime.fromisoformat(receipts[-1]['recoveryRequestedAt'])
            end=datetime.fromisoformat(after['observedAt'])
            def in_window(row):
                if not row.get('publishedAt'): return False
                stamp=datetime.fromisoformat(row['publishedAt'].replace(' ', 'T'))
                # DB DATETIME is UTC in this deployment; clock offset remains a reported limitation.
                if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=timezone.utc)
                return release<=stamp<=end
            new={r['outboxId'] for r in after['rows'] if 'outboxId'in r and r.get('status')=='PUBLISHED' and in_window(r)}
            gap_record={'startedAt':receipts[-1]['recoveryRequestedAt'],'endedAt':after['observedAt'],
                        'newTrialReceiptIds':sorted(new),'receiptNotRawKafka':True}
            f.private_write(directory/'recovery-window.json',gap_record)
            f.require(after['coverage']=='bounded-trial-only' and bool(new),
                      'No trial delivery observed in recovery window; C1 is inconclusive, do not reinject')
        # Snapshot age and target equality are checked by execute each time.
        receipt=f.execute(k,phase,phase['planSha256'],directory/f'phase-{i+1}.json',observe=observe,
                          snapshotter=kafka_snapshot if p['case']=='C2' else None)
        receipts.append(receipt)
        f.require(receipt['injectionConfirmed'],'Controller did not confirm injection')
        # Delete only the first recovered phase to permit the second. Last stays paused for audit/recovery.
        if i+1<len(p['plans']): delete_recovered(k,phase,receipt['faultUid'])
    result={'case':p['case'],'planSha256':expected,'phases':receipts,'recoveryWindow':gap_record,
            'businessRecoveryVerified':False,'automaticRecovery':None,
            'note':'Collect DB, broker receipts, Trace and alert evidence; controller recovery is not business recovery'}
    f.private_write(directory/'execution.json',result)
    return result


def collect(k, ids):
    s=f.snapshot(k)
    raw=k.call(['-n','mysql','exec','-i',s['mysql']['name'],'-c','mysql','--','bash','-c',f.DB_SHELL], f.followup_evidence_sql(ids))
    rows=[f.bounded_json(line) for line in raw.splitlines() if line]
    f.require(all(row.get('tripId') in ids for row in rows),'Unexpected trial ID')
    sections={row['section'] for row in rows}
    capped=any(sum(r['section']==name for r in rows)>200 for name in sections)
    return {'tripIds':ids,'approvedRevision':s['approvedRevision'],'observedAt':f.now(),
            'rows':rows,'coverage':'truncated' if capped else 'bounded-trial-only'}


def reconcile(accepted, final, trace):
    f.require(accepted['tripIds']==final['tripIds'] and accepted['approvedRevision']==final['approvedRevision'], 'Trial/deployment changed')
    f.require(accepted['coverage']==final['coverage']=='bounded-trial-only','Truncated evidence')
    items=[r for r in accepted['rows'] if r['section']=='trial_item']
    photos=[r for r in final['rows'] if r['section']=='final_attachment']
    final_items=[r for r in final['rows'] if r['section']=='trial_item']
    accepted_ids={r['itemId'] for r in items}
    final_item_ids={r['itemId'] for r in final_items}
    expected={r['attachmentId'] for r in final_items if r.get('attachmentId') is not None}
    actual={r['attachmentId'] for r in photos}
    delivery=[r for r in final['rows'] if r['section']=='delivery_trace']
    pending=[r['outboxId'] for r in final['rows'] if r['section'] in ('conversion_outbox','creation_outbox') and r['status']!='PUBLISHED']
    attempts={str(r['itemId']):r.get('attempt') for r in final['rows'] if r['section']=='trial_item'}
    tid=trace.get('trace',{}).get('traceId')
    parents=[r.get('traceparent','') or '' for r in delivery]
    linked=bool(trace.get('traceEvidenceReady') is True and trace.get('telemetryIntegrity')=='no_observed_loss' and
                re.fullmatch('[a-f0-9]{32}',tid or '') and parents and all(
                    re.fullmatch('00-'+tid+'-[a-f0-9]{16}-[a-f0-9]{2}',parent) for parent in parents))
    evidence={'acceptedItemCount':len(accepted_ids),'missingItemIds':sorted(accepted_ids-final_item_ids),'unexpectedItemIds':sorted(final_item_ids-accepted_ids),'acceptedAttachmentCount':len(expected),'finalAttachmentCount':len(actual),
              'missingAttachmentIds':sorted(expected-actual),'unexpectedAttachmentIds':sorted(actual-expected),
              'duplicateAttachmentRows':len(photos)-len(actual),'duplicateFileRows':len(photos)-len({r['fileId'] for r in photos}),
              'unpublishedOutboxIds':pending,'attempts':attempts,'deliveryReceipts':delivery,
              'traceEvidenceReady':linked,'rawKafkaDeliveryCount':None,'exactlyOnceProven':False}
    # No pass from row count alone; absent acceptance IDs or receipt offsets are incomplete.
    evidence['identityEvidenceComplete']=bool(expected and accepted_ids==final_item_ids and len(accepted_ids)==len(items)==len(final_items) and len(expected)==len(items) and not(expected^actual) and
        len(photos)==len(actual) and evidence['duplicateFileRows']==0 and delivery and
        all(r.get('offset') is not None and r.get('partition') is not None and r.get('traceparent') for r in delivery) and not pending and linked)
    evidence['businessRecoveryVerified']=False
    evidence['note']='Identity/receipt/Trace coverage only. Check final execution status and external side effects separately; raw broker delivery count unmeasured.'
    return evidence


def assess(case, accepted, final, trace, timing):
    """Offline report from explicit evidence, not a fabricated timer guarantee.

    Timing file contains measured UTC instants, their sources, gaps and manual actions.
    Missing fields stay unassessed. Broker arrival is not inferred from a DB receipt.
    """
    proof=reconcile(accepted,final,trace)
    limits=criteria(case)
    def delta(start,end):
        try:
            a=datetime.fromisoformat(timing[start].replace('Z','+00:00'))
            b=datetime.fromisoformat(timing[end].replace('Z','+00:00'))
            if a.tzinfo is None or b.tzinfo is None: return None
            value=(b-a).total_seconds()
            return value if math.isfinite(value) and value>=0 else None
        except (ValueError,KeyError,TypeError): return None
    detection=delta('firstActiveCreatedAt','firstFiringEvaluationAt')
    delivery=delta('releaseObservedAt','firstDeliveryObservedAt')
    completion=delta('releaseObservedAt','completedAt')
    executions=[r for r in final['rows'] if r['section']=='creation_execution']
    done=len(executions)==1 and executions[0]['status']=='COMPLETED'
    names=('firstActiveCreatedAt','firstFiringEvaluationAt','releaseObservedAt','firstDeliveryObservedAt','completedAt')
    # A second empty interruption is not evidence of a second recovery.
    windows=timing.get('faultWindows',[])
    exposure=[]
    for window in windows:
        try:
            start=datetime.fromisoformat(window['injectedObservedAt'].replace('Z','+00:00'))
            end=datetime.fromisoformat(window['recoveryRequestedAt'].replace('Z','+00:00'))
            f.require(start.tzinfo is not None and end.tzinfo is not None and start<=end,'Invalid fault window')
            exposed=[]
            for row in final['rows']:
                if row['section'] not in ('conversion_outbox','creation_outbox'): continue
                created=datetime.fromisoformat(row['createdAt'].replace(' ','T').replace('Z','+00:00'))
                if created.tzinfo is None: created=created.replace(tzinfo=timezone.utc)
                published=datetime.fromisoformat(row['publishedAt'].replace(' ','T').replace('Z','+00:00')) if row.get('publishedAt') else None
                if published is not None and published.tzinfo is None: published=published.replace(tzinfo=timezone.utc)
                if created<=end and (published is None or published>=start): exposed.append(row['outboxId'])
            exposure.append(exposed)
        except (ValueError,KeyError,TypeError): exposure.append([])
    exposed_all=len(exposure)==(2 if case=='C1' else 1) and all(exposure)
    sourced=all(isinstance(timing.get('sources',{}).get(x),str) and timing['sources'][x] for x in names)
    complete=bool(proof['identityEvidenceComplete'] and done and sourced and exposed_all and all(x is not None for x in (detection,delivery,completion)) and
                  timing.get('observationGaps')==[] and timing.get('manualActions')==[])
    return {'case':case,'approvedRevision':final['approvedRevision'], 'evidenceComplete':complete,
            'detectionSeconds':detection,'releaseToDeliverySeconds':delivery,'releaseToCompletionSeconds':completion,
            'automaticRecovery': True if complete else None,'faultWindowExposureIds':exposure,
            'detectionTargetMet': detection<=limits['detectSeconds'] if detection is not None else None,
            'deliveryTargetMet': delivery<=limits['deliverySeconds'] if delivery is not None else None,
            'completionTargetMet': completion<=limits['completionSeconds'] if completion is not None else None,
            'criteria':limits,'identity':proof,'sourceEvidenceHashes':{k:f.sha(v) for k,v in (
                ('accepted',accepted),('final',final),('trace',trace),('timing',timing))},
            'limitations':['DB created_at/publication receipt are not exact commit/broker arrival',
                           'mixed DB and observer clocks; clock-offset bound must be reported',
                           'firing evaluation is not external notification receipt',
                           'raw broker delivery count and all external duplicate effects are unmeasured']}
