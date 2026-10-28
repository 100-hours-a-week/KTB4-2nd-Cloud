import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('failure452', Path(__file__).parents[1]/'scripts/nginx-failure-evidence.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)/'session'
        m.prepare(self.root, 300)
        self.c = m.manifest(self.root)
        self.now = datetime.now(timezone.utc)

    def tearDown(self): self.tmp.cleanup()

    def put(self, relative, data, lines=False):
        (self.root/relative).write_text(''.join(json.dumps(x)+'\n' for x in data) if lines else json.dumps(data))

    def ready(self):
        for role, (field, _, error) in m.ROLES.items():
            self.put(role+'/checkpoint.json', {'runId': self.c[field], 'updatedAt': self.now.isoformat(), error: 0,
                'failures': 0, 'guardTriggered': False, 'counterDelta': {'pps_allowance_exceeded': 0},
                'packets': 3, 'discardedAfterBudget': 0, 'filterSha256': 'b'*64})
        self.put('source-binding.json', {'captureId': self.c['captureId'], 'requestId': 'a'*32,
            'confirmedAt': self.now.isoformat(), 'networkChangedSinceConfirmation': False,
            'serverPeerMatched': True, 'tcpFilterVerified': True, 'filterSha256': 'b'*64})

    def test_plan_does_not_start_collectors_and_missing_is_not_ready(self):
        self.assertEqual(self.c['state'], 'planned-only')
        self.assertFalse(self.c['loadStarted'])
        self.assertFalse(m.readiness(self.root, self.now)['serverSnapshotReady'])
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        with self.assertRaises(FileExistsError): m.prepare(self.root, 300)

    def test_fresh_roles_still_do_not_claim_phone_navigation(self):
        self.ready()
        r = m.readiness(self.root, self.now)
        self.assertTrue(r['serverSnapshotReady'])
        self.assertFalse(r['phoneNavigationCaptureVerified'])
        self.assertFalse(m.readiness(self.root, self.now+timedelta(seconds=11))['serverSnapshotReady'])

    def test_missing_tcp_changed_source_old_id_and_ended_collector_block_readiness(self):
        self.ready()
        (self.root/'tcp/checkpoint.json').unlink()
        self.assertFalse(m.readiness(self.root, self.now)['serverSnapshotReady'])
        self.ready(); p = self.root/'source-binding.json'; x=json.loads(p.read_text()); x['networkChangedSinceConfirmation']=True; p.write_text(json.dumps(x))
        self.assertFalse(m.readiness(self.root, self.now)['serverSnapshotReady'])
        self.ready(); p=self.root/'external/checkpoint.json'; x=json.loads(p.read_text()); x['runId']=self.c['normalId'];x['stopped']=True;p.write_text(json.dumps(x))
        issues=m.readiness(self.root,self.now)['checks']['external']['issues']
        self.assertIn('run-id-mismatch',issues);self.assertIn('collector-ended-or-stopping',issues)

    def test_missing_counters_filter_mismatch_future_and_tcp_budget_are_not_ready(self):
        for role, changes in [('host', {'sampleErrors': None}), ('tcp', {'discardedAfterBudget': 1}),
                              ('tcp', {'filterSha256': 'c'*64}), ('ena', {'counterDelta': {}}),
                              ('external', {'updatedAt': (self.now+timedelta(seconds=30)).isoformat()})]:
            self.ready(); p=self.root/role/'checkpoint.json';x=json.loads(p.read_text());x.update(changes);p.write_text(json.dumps(x))
            self.assertFalse(m.readiness(self.root,self.now)['serverSnapshotReady'])

    def test_id_match_is_exact_and_absence_never_means_lost_request(self):
        self.put('client/har.json', {'version':2,'origin':m.ORIGIN,'coverage':dict.fromkeys(['authorize','provider','callback','exchange','map'],True),
            'requests':[{'url':m.ORIGIN+'/api/trips/map','requestId':'a'*32},
                        {'url':m.ORIGIN+'/login','requestId':None,'failureStage':'transport-stage-unknown'}]})
        self.put('app/access.jsonl',[{'request_id':'a'*32,'status':200,'request_time':.011,
                                     'PRIVATE_COOKIE':'MUST_NOT_EXPORT'}],True)
        report=m.analyze(self.root)
        self.assertEqual(report['requestIdCorrelation'][0]['serverMatches'],1)
        self.assertEqual(report['requestIdCorrelation'][1]['serverMatches'],0)
        self.assertEqual(report['rootCause'],'undetermined')
        self.assertEqual(report['missingAuthPhases'],['authorize','provider','callback','exchange'])
        self.assertFalse(report['observation']['exactTimeKnown'])
        self.assertFalse(report['tcpComplete'])
        self.assertNotIn('MUST_NOT_EXPORT',json.dumps(report))

    def test_tcp_patterns_do_not_claim_tls_or_ordered_handshake(self):
        packets=[{'flowId':'a'*20,'direction':'in','flags':'S'},
                 {'flowId':'b'*20,'direction':'out','flags':'.'},
                 {'flowId':'c'*20,'direction':'out','flags':'S.'},
                 {'flowId':'c'*20,'direction':'in','flags':'S'}]
        self.assertEqual(m.tcp_patterns(packets),{'syn-without-synack-in-window':1,
            'no-initial-syn-in-window':1,'syn-and-synack-observed':1})

    def test_duplicate_server_ids_and_incomplete_tcp_are_preserved(self):
        self.put('client/har.json',{'version':2,'origin':m.ORIGIN,'requests':[
            {'url':m.ORIGIN+'/login','requestId':'a'*32,'failureStage':'browser-policy'}]})
        self.put('app/access.jsonl',[{'request_id':'a'*32,'status':429,'request_time':.001}]*2,True)
        self.put('tcp/packets.jsonl',[{'flowId':'a'*20,'direction':'in','flags':'S'}],True)
        self.put('tcp/summary.json',{'runId':self.c['captureId'],'complete':True,'kernelDropped':0,'packets':2})
        report=m.analyze(self.root)
        self.assertEqual(report['requestIdCorrelation'][0]['serverMatches'],2)
        self.assertEqual(report['clientFailureStages'],{'browser-policy':1})
        self.assertFalse(report['tcpComplete'])
        self.assertFalse(report['collectorSummaries']['normal']['ownedCredentialsRevoked'])

    def test_actual_observation_time_is_never_replaced_by_report_time(self):
        self.put('client/observation.json',{'reportedAt':self.now.isoformat(),'observedAt':None,
            'route':'/PRIVATE_PERSON?code=PRIVATE_TOKEN'})
        report=m.analyze(self.root)
        self.assertEqual(report['observation'],{'observedAt':None,'exactTimeKnown':False,'route':'[other]'})
        self.assertNotIn('PRIVATE_',json.dumps(report))


if __name__ == '__main__': unittest.main()
