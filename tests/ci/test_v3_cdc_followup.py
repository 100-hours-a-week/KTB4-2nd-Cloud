import copy
import importlib.util
import json
import os
import subprocess
import yaml
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
sys.path.insert(0,str(ROOT/'tests/ci'))
import v3_cdc_fault as f
import v3_cdc_followup as m
from test_v3_cdc_fault import fixture, FakeKube
spec=importlib.util.spec_from_file_location('outbox694',ROOT/'deploy/v3/metrics/files/outbox-metrics.py')
outbox=importlib.util.module_from_spec(spec);spec.loader.exec_module(outbox)


def kafka():
    s=fixture()
    s['kafka']={'port':9093,'serviceIp':'172.20.20.15','serviceUid':'9'*8+'-1111-1111-1111-111111111111',
                'image':'ghcr.io/100-hours-a-week/yeodam-v3-connect@sha256:'+'a'*64,
                'timers':dict(m.WORKER_TIMERS),'connectReplicas':1,'brokers':[]}
    for i in range(3):
        s['kafka']['brokers'].append({'name':m.KAFKA+'-dual-role-'+str(i),'uid':str(i+6)*8+'-1111-1111-1111-111111111111',
            'ip':'10.40.20.'+str(i+10),'node':'cpu-'+str(i),'ownerKind':'StrimziPodSet','ownerName':m.KAFKA+'-dual-role'})
    return s


class Followup(unittest.TestCase):
    def test_exact_kafka_9093_never_broker_peer_or_mysql(self):
        p=m.prepare(kafka(),'cdc-output','C2');m.verify(p,p['planSha256'])
        spec=p['plans'][0]['fault']['spec']
        self.assertEqual(spec['externalTargets'],['10.40.20.10:9093','10.40.20.11:9093','10.40.20.12:9093','172.20.20.15:9093'])
        self.assertEqual(spec['selector']['pods'],{'kafka':[fixture()['connect']['name']]})
        self.assertNotIn('target',spec)
        for change in ('port','uid','ip','owner','timer','scheduled-timer','replicas'):
            s=kafka()
            if change=='port':s['kafka']['port']=9091
            if change=='uid':s['kafka']['brokers'][0]['uid']='foreign'
            if change=='ip':s['kafka']['brokers'][0]['ip']='8.8.8.8'
            if change=='owner':s['kafka']['brokers'][0]['ownerName']='other'
            if change=='scheduled-timer':s['kafka']['timers']['scheduled.rebalance.max.delay.ms']=300000
            if change=='replicas':s['kafka']['connectReplicas']=2
            if change=='timer':s['kafka']['timers']['producer.delivery.timeout.ms']=120000
            with self.subTest(change=change), self.assertRaises(ValueError):m.prepare(s,'cdc-output','C2')

    def test_named_broker_tls_port_requires_unique_9093_on_each_verified_pod(self):
        records=kafka()['kafka']['brokers']
        pods=[{'metadata':{'name':p['name'],'labels':{'strimzi.io/pool-name':'dual-role'}},
               'spec':{'containers':[{'ports':[{'name':'tcp-clientstls','containerPort':9093,'protocol':'TCP'}]}]}}for p in records]
        service={'metadata':{'uid':kafka()['kafka']['serviceUid']},'spec':{'clusterIP':'172.20.20.15','ports':[{'port':9093,'targetPort':'tcp-clientstls','protocol':'TCP'}]}}
        slices={'items':[{'ports':[{'port':9093,'protocol':'TCP'}],'endpoints':[{'conditions':{'ready':True},'targetRef':{'kind':'Pod','uid':p['uid']},'addresses':[p['ip']]}for p in records]}]}
        helm=os.environ.get('V3_HELM')
        if not helm:self.skipTest('Set pinned V3_HELM for the actual merged Connect Chart profile')
        rendered=subprocess.check_output([helm,'template','yeodam-v3-connect',str(ROOT/'deploy/v3/connect'),'-n','default','-f',str(ROOT/'tests/ci/v3/connect.values.fixture.yaml')],text=True)
        connect=next(x for x in yaml.safe_load_all(rendered) if x and x['kind']=='KafkaConnect')
        class Kube:
            def get(self,kind,*args,**kwargs):
                if kind=='pods':return {'items':pods}
                if kind=='service':return service
                if kind=='endpointslices':return slices
                if kind=='kafkaconnect':return connect
                if kind=='kafkaconnector':return {'spec':{'config':{}}}
                raise AssertionError(kind)
        def record(p):return next(x for x in records if x['name']==p['metadata']['name'])
        with patch.object(f,'snapshot',return_value=fixture()),patch.object(f,'pod_record',side_effect=record):
            self.assertEqual(m.kafka_snapshot(Kube())['kafka']['port'],9093)
            # Match the merged Connect Chart profile, then reject stale or widened profiles
            # before preparing a fault. Zero is never inferred from a missing override.
            config=connect['spec']['config']
            for bad in (None,300000,10000,False,'0',-1):
                if bad is None:config.pop('scheduled.rebalance.max.delay.ms',None)
                else:config['scheduled.rebalance.max.delay.ms']=bad
                with self.subTest(timer=bad),self.assertRaises(ValueError):m.kafka_snapshot(Kube())
            config['scheduled.rebalance.max.delay.ms']=0
            for bad in (None,2,True,'1'):
                connect['spec']['replicas']=bad
                with self.subTest(replicas=bad),self.assertRaises(ValueError):m.kafka_snapshot(Kube())
            connect['spec']['replicas']=1
            config['worker.unsync.backoff.ms']=0
            with self.assertRaises(ValueError):m.kafka_snapshot(Kube())
            config.pop('worker.unsync.backoff.ms')
            snap=m.kafka_snapshot(Kube());plan=m.prepare(snap,'cdc-reviewed-zero-delay','C2');m.verify(plan,plan['planSha256'])
            self.assertEqual(plan['plans'][0]['seconds'],90)
            self.assertEqual(plan['criteria'],m.criteria('C2'))
            original=copy.deepcopy(pods)
            for bad in ('wrong-port','duplicate','unreviewed-name','foreign-endpoint'):
                pods[:]=copy.deepcopy(original);service['spec']['ports'][0]['targetPort']='tcp-clientstls'
                if bad=='wrong-port':pods[1]['spec']['containers'][0]['ports'][0]['containerPort']=9092
                if bad=='duplicate':pods[0]['spec']['containers'][0]['ports']*=2
                if bad=='unreviewed-name':service['spec']['ports'][0]['targetPort']='http'
                if bad=='foreign-endpoint':slices['items'][0]['endpoints'][0]['targetRef']['uid']='foreign'
                with self.subTest(bad=bad),self.assertRaises(ValueError):m.kafka_snapshot(Kube())

    def test_replaced_broker_requires_new_plan_even_same_ip(self):
        p=m.prepare(kafka(),'cdc-output','C2')['plans'][0];live=kafka()
        live['kafka']['brokers'][0]['uid']='f'*8+'-1111-1111-1111-111111111111'
        with self.assertRaises(ValueError):f.compare_live(p,live)

    def test_intermit_requires_current_c0_and_two_reconnection_windows(self):
        s=fixture();c={'case':'C0','automaticRecovery':True,'evidenceComplete':True,'approvedRevision':s['approvedRevision'],'releaseToDeliverySeconds':10.5,'criteria':m.criteria('C0'),'sourceEvidenceHashes':dict.fromkeys(('accepted','final','trace','timing'),'a'*64)}
        p=m.prepare(s,'cdc-flap','C1',c);m.verify(p,p['planSha256'])
        self.assertEqual([x['seconds']for x in p['plans']],[45,45]);self.assertEqual(p['recoveryWindowSeconds'],30)
        c['releaseToDeliverySeconds']=21;p=m.prepare(s,'cdc-flap','C1',c);self.assertEqual(p['recoveryWindowSeconds'],50)
        for key,val in [('automaticRecovery',False),('approvedRevision','b'*40),('releaseToDeliverySeconds',float('nan')),('releaseToDeliverySeconds',31),('evidenceComplete',False)]:
            bad=dict(c);bad[key]=val
            with self.subTest(key=key),self.assertRaises(ValueError):m.prepare(s,'cdc-flap','C1',bad)

    def test_rehashed_changed_case_or_threshold_is_rejected(self):
        p=m.prepare(fixture(),'cdc-base','C0');p['criteria']['detectSeconds']=999
        p['planSha256']=f.sha({k:v for k,v in p.items() if k!='planSha256'})
        with self.assertRaises(ValueError):m.verify(p,p['planSha256'])

    def test_observer_failure_blocks_before_create(self):
        p=f.prepare(fixture(),'cdc-fail-closed',15);k=FakeKube(p)
        with tempfile.TemporaryDirectory() as t,patch.object(f,'snapshot',return_value=p['snapshot']):
            with self.assertRaises(ValueError):f.execute(k,p,p['planSha256'],Path(t)/'r.json',observe=lambda:False)
        self.assertFalse(any(a[0]=='create'for a in k.actions))

    def test_observer_failure_during_fault_recovers_owned_resource(self):
        p=f.prepare(fixture(),'cdc-observer-loss',15);k=FakeKube(p);calls=iter([True,False])
        with tempfile.TemporaryDirectory() as t,patch.object(f,'snapshot',return_value=p['snapshot']):
            with self.assertRaises(ValueError):f.execute(k,p,p['planSha256'],Path(t)/'r.json',observe=lambda:next(calls))
            receipt=json.loads((Path(t)/'r.json.finished.json').read_text())
            self.assertTrue(receipt['recovery']['controllerReportedRecovered'])
            self.assertIn('recoveryRequestedAt',receipt)
        self.assertEqual(k.obj['metadata']['annotations'][f.PAUSE],'true')

    def test_metadata_is_minimal_and_schema_names_are_real(self):
        sql=f.followup_evidence_sql([123])
        self.assertIn('i.conversion_attempts',sql);self.assertNotIn('i.status',sql)
        for forbidden in ('original_file_name','object_key','payload','tracestate','user_id'):
            self.assertNotIn(forbidden,sql)
        self.assertEqual(sql.count('LIMIT 201'),7)
        self.assertTrue(sql.endswith('COMMIT;\n'))

    def test_receipts_do_not_prove_raw_delivery_or_exactly_once(self):
        a={'tripIds':[1],'approvedRevision':'a'*40,'coverage':'bounded-trial-only','rows':[{'section':'trial_item','itemId':7,'attachmentId':None}]}
        b=dict(a,rows=[{'section':'trial_item','itemId':7,'attachmentId':9,'attempt':2},
            {'section':'final_attachment','attachmentId':9,'fileId':3},
            {'section':'delivery_trace','outboxId':'out','offset':12,'partition':0,'traceparent':'00-'+'a'*32+'-'+'b'*16+'-01'}])
        trace={'traceEvidenceReady':True,'telemetryIntegrity':'no_observed_loss','trace':{'traceId':'a'*32}}
        result=m.reconcile(a,b,trace)
        self.assertTrue(result['identityEvidenceComplete']);self.assertFalse(result['exactlyOnceProven'])
        self.assertFalse(result['businessRecoveryVerified']);self.assertIsNone(result['rawKafkaDeliveryCount'])
        for rows in (b['rows']+[b['rows'][1]], [dict(x,itemId=8) if x['section']=='trial_item' else x for x in b['rows']]):
            self.assertFalse(m.reconcile(a,dict(b,rows=rows),trace)['identityEvidenceComplete'])
        self.assertFalse(m.reconcile(a,b,{})['identityEvidenceComplete'])
        wrong=dict(trace,trace={'traceId':'c'*32});self.assertFalse(m.reconcile(a,b,wrong)['identityEvidenceComplete'])

    def test_query_timing_is_wall_time_and_invalid_samples_are_unknown(self):
        rows=[t+'\t0\t0'for t in outbox.TABLES]+['conversion_'+c+'\t0\t0'for c in outbox.CLASSES]
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'snapshot'
            for first,ok in [('100.1\t100.314509',True),('110\t100',False),('NaN\t100',False),('90\t100',False)]:
                path.write_text(first+'\n'+'\n'.join(rows)+'\n');s=outbox.metrics(path,110)
                self.assertEqual('yeodam_outbox_probe_success 1' in s,ok)
                if ok:self.assertIn('yeodam_outbox_query_duration_seconds 0.214509',s)

    def test_time_targets_separate_observations_timers_and_policy(self):
        c=m.criteria('C0');self.assertEqual(sum(c['detectBudget'].values()),c['detectSeconds'])
        self.assertEqual(c['baselineSamples'],3);self.assertFalse(c['statisticalSlo'])
        self.assertEqual(c['completionSeconds'],210+c['deliverySeconds']+30)
        k=m.criteria('C2');self.assertNotEqual(k['deliverySeconds'],c['deliverySeconds'])
        self.assertEqual(m.WORKER_TIMERS['producer.delivery.timeout.ms'],2147483647)

    def test_dead_local_observer_is_unknown_and_persists_evidence(self):
        with tempfile.TemporaryDirectory() as t:
            o=m.Observer('http://127.0.0.1:1',t)
            with patch.object(o,'get',side_effect=OSError()):self.assertFalse(o())
            record=json.loads((Path(t)/'observation-00001.json').read_text());self.assertFalse(record['healthy'])
            for base in ('https://example.com','http://127.0.0.1:80/path','http://a@localhost:80'):
                with self.assertRaises(ValueError):m.Observer(base,t)

    def test_empty_recovery_window_never_starts_second_fault(self):
        s=fixture();c={'case':'C0','automaticRecovery':True,'evidenceComplete':True,
            'approvedRevision':s['approvedRevision'],'releaseToDeliverySeconds':10.5,
            'criteria':m.criteria('C0'),'sourceEvidenceHashes':dict.fromkeys(('accepted','final','trace','timing'),'a'*64)}
        p=m.prepare(s,'cdc-gap','C1',c)
        receipt={'injectionConfirmed':True,'faultUid':'owned','recoveryRequestedAt':f.now()}
        empty={'rows':[],'observedAt':f.now(),'coverage':'bounded-trial-only'}
        with tempfile.TemporaryDirectory() as t,patch.object(f,'execute',return_value=receipt) as execute, \
             patch.object(m,'delete_recovered'),patch.object(f,'collect_db',return_value=empty), \
             patch.object(m.time,'monotonic',side_effect=[0,31]):
            with self.assertRaises(f.GuardError):m.run(None,p,p['planSha256'],t,lambda:True,[1])
            self.assertEqual(execute.call_count,1)
            self.assertEqual(json.loads((Path(t)/'recovery-window.json').read_text())['newTrialReceiptIds'],[])

    def test_assessment_requires_exposed_work_final_success_and_trace(self):
        stamp=lambda n:'2026-11-14T00:'+str(n//60).zfill(2)+':'+str(n%60).zfill(2)+'+00:00'
        a={'tripIds':[1],'approvedRevision':'a'*40,'coverage':'bounded-trial-only','rows':[{'section':'trial_item','itemId':7,'attachmentId':None}]}
        b=dict(a,rows=[{'section':'trial_item','itemId':7,'attachmentId':9,'attempt':1},
            {'section':'final_attachment','attachmentId':9,'fileId':3},
            {'section':'delivery_trace','outboxId':'o','offset':12,'partition':0,'traceparent':'00-'+'a'*32+'-'+'b'*16+'-01'},
            {'section':'conversion_outbox','outboxId':'o','status':'PUBLISHED','createdAt':stamp(10),'publishedAt':stamp(100)},
            {'section':'creation_execution','status':'COMPLETED'}])
        trace={'traceEvidenceReady':True,'telemetryIntegrity':'no_observed_loss','trace':{'traceId':'a'*32}}
        timing={'firstActiveCreatedAt':stamp(10),'firstFiringEvaluationAt':stamp(60),'releaseObservedAt':stamp(90),
                'firstDeliveryObservedAt':stamp(100),'completedAt':stamp(290),'manualActions':[],'observationGaps':[],
                'faultWindows':[{'injectedObservedAt':stamp(0),'recoveryRequestedAt':stamp(90)}]}
        timing['sources']={k:'synthetic-proof-file' for k in timing if k.endswith('At')}
        result=m.assess('C0',a,b,trace,timing)
        self.assertTrue(result['automaticRecovery']);self.assertEqual(result['detectionSeconds'],50)
        self.assertTrue(result['completionTargetMet']);self.assertEqual(len(result['sourceEvidenceHashes']),4)
        for key,value in [('observationGaps',['gap']),('manualActions',['restart']),('sources',{}),('faultWindows',[])]:
            self.assertIsNone(m.assess('C0',a,b,trace,dict(timing,**{key:value}))['automaticRecovery'])
        # A later empty second phase must never become evidence of two recoveries.
        second={'injectedObservedAt':stamp(120),'recoveryRequestedAt':stamp(165)}
        self.assertFalse(m.assess('C1',a,b,trace,dict(timing,faultWindows=timing['faultWindows']+[second]))['evidenceComplete'])

    def test_server_clock_skew_does_not_hide_fresh_or_stale_data(self):
        def samples(stamp=999):
            vals={'up':1,'yeodam_outbox_probe_success':1,
                  'yeodam_outbox_query_started_timestamp_seconds':998.8,
                  'yeodam_outbox_query_completed_timestamp_seconds':999,
                  'yeodam_outbox_query_duration_seconds':.2}
            return {'status':'success','data':{'result':[{'metric':{'__name__':n},'value':[stamp,str(v)]}for n,v in vals.items()]}}
        rule={'status':'success','data':{'groups':[{'rules':[{'name':'V3ConversionDeliveryStalled','health':'ok','state':'inactive','lastEvaluation':'1970-01-01T00:16:39+00:00'}]}]}}
        scrape={'status':'success','data':{'result':[{'value':[1000,'999']}]}}
        clock={'status':'success','data':{'result':[{'value':[1000,'1000']}]}}
        for metric_stamp,expected in ((999,True),(974,False),(1001,False)):
            with self.subTest(stamp=metric_stamp),tempfile.TemporaryDirectory()as t:
                o=m.Observer('http://127.0.0.1:12345',t)
                with patch.object(o,'get',side_effect=[samples(metric_stamp),rule,scrape,clock]),patch.object(m.time,'time',side_effect=[9000,9000.1]):
                    self.assertEqual(o(),expected)
                evidence=json.loads((Path(t)/'observation-00001.json').read_text())
                self.assertIn('clock',evidence)
                self.assertLess(evidence['clock']['serverMinusLocalIntervalSeconds'][1],0)

class ObservationClock:
    def __init__(self): self.value=100.0
    def monotonic(self): return self.value
    def sleep(self, seconds): self.value+=seconds


class ObservationKube:
    def __init__(self, clock, results, identity_seconds=0):
        self.clock,self.results,self.calls=clock,list(results),[]
        self.identity_seconds=identity_seconds
        self.pod={'metadata':{'uid':fixture()['mysql']['uid'],'ownerReferences':[
            {'kind':'StatefulSet','name':'mysql','controller':True}]},'spec':{},
            'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}}
        self.rows=[{'section':'execution_timing','tripId':1,'status':'COMPLETED',
                    'endedAt':'2026-11-15 03:46:13.247857'},
                   {'section':'db_clock','tripId':1,'utc':'2026-11-15 03:47:33.863688'}]

    def call(self,args,source=None,timeout=20):
        self.calls.append((args,source,timeout))
        if 'get' in args:
            self.clock.sleep(min(timeout,self.identity_seconds))
            return json.dumps(self.pod)
        assert 'exec' in args and source.startswith('SET SESSION MAX_EXECUTION_TIME=3000; START TRANSACTION READ ONLY;')
        result=self.results.pop(0)
        if result=='timeout':
            self.clock.sleep(timeout)
            raise subprocess.TimeoutExpired(['DO-NOT-EXPORT-RAW-COMMAND'],timeout,output='secret-row')
        if isinstance(result,BaseException): raise result
        return '\n'.join(json.dumps(r) for r in (self.rows if result=='success' else result))


class DbObservationTests(unittest.TestCase):
    def setup_observer(self, directory, results, identity_seconds=0):
        os.chmod(directory,0o700)
        clock=ObservationClock();k=ObservationKube(clock,results,identity_seconds)
        return clock,k,m.DbObserver(k,fixture(),1,directory)

    def test_timeout_then_success_preserves_gap_and_blocks_reinjection(self):
        with tempfile.TemporaryDirectory() as t:
            clock,k,o=self.setup_observer(t,['timeout','success','success'])
            with patch.object(m.time,'monotonic',clock.monotonic),patch.object(m.time,'sleep',clock.sleep):
                proof=o.sample()
                self.assertEqual(proof['attempt'],2)
                self.assertEqual(clock.value,121)
                self.assertFalse(m.db_observer_ready(t,1,fixture()['approvedRevision']))
                # Future observation is allowed even though additional injection is latched off.
                o.sample()
            summary=o.summary('completed')
            self.assertFalse(summary['continuousObservationComplete'])
            self.assertFalse(summary['allowNextInjection'])
            self.assertEqual(len(summary['sampleFiles']),2)
            self.assertEqual(len(summary['observationGaps']),1)
            text=''.join(p.read_text() for p in Path(t).glob('*.json'))
            self.assertNotIn('DO-NOT-EXPORT',text);self.assertNotIn('secret-row',text)
            self.assertIn('gap-recovered',str(list(Path(t).iterdir())))
            self.assertTrue(all(p.stat().st_mode&0o077==0 for p in Path(t).iterdir()))

    def test_persistent_timeout_exhausts_two_reads_without_mutation(self):
        with tempfile.TemporaryDirectory() as t:
            clock,k,o=self.setup_observer(t,['timeout','timeout'])
            with patch.object(m.time,'monotonic',clock.monotonic),patch.object(m.time,'sleep',clock.sleep):
                with self.assertRaises(m.DbObservationUnavailable):o.sample()
                with self.assertRaises(f.GuardError):o.sample()
            self.assertEqual(len([c for c in k.calls if 'exec' in c[0]]),2)
            self.assertLessEqual(clock.value-100,45)
            self.assertFalse(any(set(c[0])&{'create','patch','delete','apply'} for c in k.calls))

    def test_shared_budget_includes_identity_and_backoff(self):
        with tempfile.TemporaryDirectory() as t:
            clock,k,o=self.setup_observer(t,['timeout','timeout'],identity_seconds=5)
            with patch.object(m.time,'monotonic',clock.monotonic),patch.object(m.time,'sleep',clock.sleep):
                with self.assertRaises(m.DbObservationUnavailable):o.sample()
            self.assertEqual([c[2] for c in k.calls if 'exec' in c[0]],[20,14])
            self.assertEqual(clock.value-100,45)

    def test_permission_or_identity_failure_is_not_retried(self):
        for changed in (True,False):
            with self.subTest(changed=changed),tempfile.TemporaryDirectory() as t:
                clock,k,o=self.setup_observer(t,[f.GuardError('permission refused')])
                if changed:k.pod['metadata']['uid']='changed'
                with self.assertRaises(m.DbObservationUnavailable):o.sample()
                self.assertEqual(len([c for c in k.calls if 'exec' in c[0]]),0 if changed else 1)

    def test_foreign_truncated_and_malformed_data_are_not_zero_or_retryable(self):
        for bad in ([{'section':'db_clock','tripId':2}],
                    [{'section':'trial_item','tripId':1}]*201,
                    [{'section':'unknown','tripId':1}]):
            with self.subTest(bad=len(bad)),tempfile.TemporaryDirectory() as t:
                clock,k,o=self.setup_observer(t,[bad])
                with self.assertRaises(m.DbObservationUnavailable):o.sample()
                self.assertEqual(o.samples,[])
                self.assertTrue((Path(t)/'blocked.json').exists())

    def test_success_interlock_is_fresh_and_trial_bound(self):
        with tempfile.TemporaryDirectory() as t:
            clock,k,o=self.setup_observer(t,['success'])
            with patch.object(m.time,'monotonic',clock.monotonic):
                o.sample()
                self.assertTrue(m.db_observer_ready(t,1,fixture()['approvedRevision']))
                self.assertFalse(m.db_observer_ready(t,2,fixture()['approvedRevision']))
                self.assertFalse(m.db_observer_ready(t,1,'b'*40))
                clock.value+=31
                self.assertFalse(m.db_observer_ready(t,1,fixture()['approvedRevision']))
                clock.value=99
                self.assertFalse(m.db_observer_ready(t,1,fixture()['approvedRevision']))

    def test_failed_db_interlock_invokes_existing_fault_finally_recovery(self):
        phase=f.prepare(fixture(),'cdc-db-observer',15);k=FakeKube(phase)
        with tempfile.TemporaryDirectory() as t,tempfile.TemporaryDirectory() as receipts:
            clock,db,o=self.setup_observer(t,['success','timeout','timeout'])
            calls=0
            def healthy():
                nonlocal calls
                calls+=1
                if calls==1:o.sample()
                else:
                    with self.assertRaises(m.DbObservationUnavailable):o.sample()
                return m.db_observer_ready(t,1,fixture()['approvedRevision'])
            with patch.object(f,'snapshot',return_value=phase['snapshot']), \
                 patch.object(m.time,'monotonic',clock.monotonic),patch.object(m.time,'sleep',clock.sleep):
                with self.assertRaises(f.GuardError):
                    f.execute(k,phase,phase['planSha256'],Path(receipts)/'r.json',observe=healthy)
            record=json.loads((Path(receipts)/'r.json.finished.json').read_text())
            self.assertTrue(record['recovery']['controllerReportedRecovered'])
            self.assertEqual(k.obj['metadata']['annotations'][f.PAUSE],'true')

    def test_gap_attachment_cannot_promote_strict_assessment(self):
        with tempfile.TemporaryDirectory() as t:
            clock,k,o=self.setup_observer(t,['timeout','success'])
            with patch.object(m.time,'monotonic',clock.monotonic),patch.object(m.time,'sleep',clock.sleep):o.sample()
            a={'tripIds':[1],'approvedRevision':fixture()['approvedRevision']}
            timing={'observationGaps':[]}
            attached=m.with_db_observations(timing,o.summary('completed'),a,a)
            self.assertTrue(attached['observationGaps'])
            self.assertEqual(timing['observationGaps'],[])
            with self.assertRaises(f.GuardError):
                m.with_db_observations(timing,o.summary('completed'),dict(a,tripIds=[2]),a)
            proof={'identityEvidenceComplete':True}
            stamp='2026-11-15T00:00:00+00:00'
            timed=dict(attached,manualActions=[],firstActiveCreatedAt=stamp,firstFiringEvaluationAt=stamp,
                       releaseObservedAt=stamp,firstDeliveryObservedAt=stamp,completedAt=stamp,
                       sources=dict.fromkeys(('firstActiveCreatedAt','firstFiringEvaluationAt','releaseObservedAt',
                                              'firstDeliveryObservedAt','completedAt'),'source'),
                       faultWindows=[{'injectedObservedAt':stamp,'recoveryRequestedAt':stamp}])
            final=dict(a,rows=[{'section':'creation_execution','status':'COMPLETED'},
                              {'section':'conversion_outbox','outboxId':'o','createdAt':stamp}])
            with patch.object(m,'reconcile',return_value=proof):result=m.assess('C2',a,final,{},timed)
            self.assertFalse(result['evidenceComplete']);self.assertIsNone(result['automaticRecovery'])
            self.assertTrue(result['completionTargetMet'])

    def test_cli_observe_persists_summary_on_both_success_and_failure(self):
        spec=importlib.util.spec_from_file_location('follow_cli',ROOT/'scripts/prepare-v3-cdc-followup.py')
        cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
        for results,ok in ((['success'],True),(['timeout','timeout'],False)):
            with self.subTest(ok=ok),tempfile.TemporaryDirectory() as t:
                os.chmod(t,0o700);clock=ObservationClock();k=ObservationKube(clock,results)
                args=['tool','observe','--kubeconfig','synthetic','--trip-id','1','--directory',t,
                      '--seconds','60','--ack-trial-metadata-read']
                with patch.object(sys,'argv',args),patch.object(f,'Kube',return_value=k), \
                     patch.object(f,'snapshot',return_value=fixture()), \
                     patch.object(m.time,'monotonic',clock.monotonic),patch.object(m.time,'sleep',clock.sleep):
                    if ok:cli.main()
                    else:
                        with self.assertRaises(m.DbObservationUnavailable):cli.main()
                summary=json.loads((Path(t)/'summary.json').read_text())
                self.assertEqual(summary['state'],'completed' if ok else 'observation_failed')
                self.assertEqual(summary['continuousObservationComplete'],ok)

    def test_cli_whole_deadline_is_not_renewed_for_a_retry(self):
        spec=importlib.util.spec_from_file_location('follow_cli_deadline',ROOT/'scripts/prepare-v3-cdc-followup.py')
        cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
        with tempfile.TemporaryDirectory() as t:
            os.chmod(t,0o700);clock=ObservationClock();k=ObservationKube(clock,['timeout'])
            args=['tool','observe','--kubeconfig','synthetic','--trip-id','1','--directory',t,
                  '--seconds','1','--ack-trial-metadata-read']
            with patch.object(sys,'argv',args),patch.object(f,'Kube',return_value=k), \
                 patch.object(f,'snapshot',return_value=fixture()), \
                 patch.object(m.time,'monotonic',clock.monotonic),patch.object(m.time,'sleep',clock.sleep):
                with self.assertRaises(m.DbObservationUnavailable):cli.main()
            self.assertEqual(clock.value,101)
            self.assertEqual(len([c for c in k.calls if 'exec' in c[0]]),1)

    def test_real_subprocess_timeout_recovers_using_only_fixed_read_sql(self):
        with tempfile.TemporaryDirectory() as t,tempfile.TemporaryDirectory() as output:
            os.chmod(output,0o700)
            fake=ObservationKube(ObservationClock(),[])
            stub=Path(t)/'kubectl-stub.py';count=Path(t)/'read-count'
            stub.write_text('import json,sys,time\nfrom pathlib import Path\n'
                +f'pod={fake.pod!r}\nrows={fake.rows!r}\n'
                +'if "get" in sys.argv: print(json.dumps(pod))\n'
                +'else:\n'
                +' sql=sys.stdin.read();assert "START TRANSACTION READ ONLY" in sql and "COMMIT;" in sql\n'
                +' p=Path(sys.argv[1]);n=int(p.read_text()) if p.exists() else 0;p.write_text(str(n+1))\n'
                +' if n==0: time.sleep(1.5)\n'
                +' print("\\n".join(json.dumps(r) for r in rows))\n')
            k=f.Kube('unused');k.base=[sys.executable,str(stub),str(count)]
            with patch.object(m.DbObserver,'CALL_SECONDS',.5),patch.object(m.DbObserver,'SAMPLE_SECONDS',3), \
                 patch.object(m.DbObserver,'RETRY_SECONDS',.01):
                o=m.DbObserver(k,fixture(),1,output);proof=o.sample()
            self.assertEqual(proof['attempt'],2)
            self.assertEqual(count.read_text(),'2')
            self.assertEqual(len(o.gaps),1)
            self.assertFalse(m.db_observer_ready(output,1,fixture()['approvedRevision']))

    def test_cli_run_binds_real_nested_plan_revision_to_db_interlock(self):
        spec=importlib.util.spec_from_file_location('follow_cli_run',ROOT/'scripts/prepare-v3-cdc-followup.py')
        cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
        with tempfile.TemporaryDirectory() as t,tempfile.TemporaryDirectory() as output, \
             tempfile.TemporaryDirectory() as inputs:
            clock,k,o=self.setup_observer(t,['success'])
            plan=m.prepare(kafka(),'cdc-703-wire','C2')
            path=Path(inputs)/'plan.json';path.write_text(json.dumps(plan))
            args=['tool','run','--kubeconfig','synthetic','--trip-id','1','--directory',output,
                  '--plan',str(path),'--approve-plan-sha256',plan['planSha256'],
                  '--ack-shared-cdc-maintenance','--prometheus','http://127.0.0.1:19091',
                  '--db-observation-directory',t]
            checked=[]
            def execute(k,phase,expected,path,observe,snapshotter):
                checked.append(observe())
                return {'injectionConfirmed':True,'faultUid':'owned'}
            with patch.object(sys,'argv',args),patch.object(f,'Kube',return_value=k), \
                 patch.object(m,'Observer',return_value=lambda:True),patch.object(f,'execute',side_effect=execute), \
                 patch.object(m.time,'monotonic',clock.monotonic):
                o.sample();cli.main()
            self.assertEqual(checked,[True])
            self.assertTrue((Path(output)/'execution.json').exists())


if __name__=='__main__':unittest.main()
