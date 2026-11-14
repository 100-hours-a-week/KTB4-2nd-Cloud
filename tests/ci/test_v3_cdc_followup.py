import copy
import importlib.util
import json
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
                'timers':dict(m.WORKER_TIMERS),'brokers':[]}
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
        for change in ('port','uid','ip','owner','timer'):
            s=kafka()
            if change=='port':s['kafka']['port']=9091
            if change=='uid':s['kafka']['brokers'][0]['uid']='foreign'
            if change=='ip':s['kafka']['brokers'][0]['ip']='8.8.8.8'
            if change=='owner':s['kafka']['brokers'][0]['ownerName']='other'
            if change=='timer':s['kafka']['timers']['producer.delivery.timeout.ms']=120000
            with self.subTest(change=change), self.assertRaises(ValueError):m.prepare(s,'cdc-output','C2')

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

if __name__=='__main__':unittest.main()
