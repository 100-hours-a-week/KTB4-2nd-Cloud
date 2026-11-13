"""Evidence semantics and bounded reads; no cluster, AWS, traffic or photo uploads."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
import v3_scaling_evidence as e


def sample(count=1, new=False):
    def resource(uid,node=None,iid=None):
        return {'uid':uid,'node':node,'instanceId':iid,'conditions':{'Ready':{'status':'True'}}}
    pods=[resource('pod1','worker1')]
    nodes=[resource('node1',iid='i-'+'1'*17)]
    if new:
        pods.append(resource('pod2','worker2'));nodes.append(resource('node2',iid='i-'+'2'*17))
    return {'sampleStartedAt':'2026-11-13T00:00:00Z','observedAt':'2026-11-13T00:00:01Z',
            'policySha256':'a'*64,'approvedRevision':'b'*40,'converter':{'ready':count},
            'hpa':{'desired':count},'pods':pods,'nodes':nodes,'nodeClaims':[]}


class ScalingEvidence(unittest.TestCase):
    def test_capacity_change_separate_from_configuration_and_unknown_policy(self):
        a={'apps':[{'name':'converter','replicas':1,'podTemplateSha256':'same'}],
           'approvedRevision':'a','nodeTypes':{'small':1},'scalingPolicies':{'policy':'fixed'}}
        b=copy.deepcopy(a);b['apps'][0]['replicas']=2;b['nodeTypes']['small']=2
        result=e.compare_conditions(a,b)
        self.assertTrue(result['staticConditionsMatch']);self.assertTrue(result['nodeTypesChanged'])
        self.assertEqual(result['replicaChanges']['converter']['after'],2)
        b['apps'][0]['podTemplateSha256']='changed';self.assertFalse(e.compare_conditions(a,b)['appConfigurationMatch'])
        b=copy.deepcopy(a);b['scalingPolicies']['policy']='changed'
        self.assertFalse(e.compare_conditions(a,b)['staticConditionsMatch'])
        del b['scalingPolicies'];self.assertIsNone(e.compare_conditions(a,b)['staticConditionsMatch'])

    def test_one_two_one_is_observation_not_business_or_ec2_success(self):
        rows=[sample(),sample(2,True),sample()];result=e.summarize(rows)
        self.assertTrue(result['continuousOneTwoOneObserved']);self.assertTrue(result['twoReadyOnDistinctNodesObserved'])
        self.assertEqual(result['removedNewWorkerInstanceIds'],['i-'+'2'*17])
        self.assertEqual(e.summarize([sample(),sample(2,True)])['remainingNewWorkerInstanceIds'],['i-'+'2'*17])
        self.assertFalse(result['ec2TerminationVerified']);self.assertFalse(result['businessCompletionVerified'])
        self.assertEqual(result['verdict'],'not_assessed')
        self.assertTrue(all('notBefore'in x and 'observedBy'in x for x in result['events']))

    def test_gap_does_not_hide_policy_change_or_prove_continuity(self):
        end=sample();end['policySha256']='changed'
        r=e.summarize([sample(),sample(2,True),{'error':'query_failed'},end])
        self.assertTrue(r['policyOrRevisionChanged']);self.assertTrue(r['observationGaps'])
        self.assertFalse(r['continuousOneTwoOneObserved'])
        unknown=sample();unknown['converter']['ready']=None
        self.assertTrue(e.summarize([sample(),unknown])['observationGaps'])

    def fixture_reader(self):
        dep={'metadata':{'generation':1},'spec':{'replicas':1,'selector':{'matchLabels':{'app.kubernetes.io/instance':'photo-converter'}},'template':{'spec':{'containers':[{'env':[{'value':'PRIVATE_ENV'}]}]}}},'status':{'observedGeneration':1,'readyReplicas':1}}
        so={'metadata':{'uid':'so'},'spec':{'scaleTargetRef':{'name':'photo-converter'},'minReplicaCount':1,'maxReplicaCount':2}}
        h={'metadata':{'uid':'h','ownerReferences':[{'uid':'so'}]},'spec':{'scaleTargetRef':{'name':'photo-converter'},'minReplicas':1,'maxReplicas':2},'status':{'currentReplicas':1,'desiredReplicas':1,'conditions':[{'type':'ScalingActive','status':'True','message':'PRIVATE_MESSAGE'}]}}
        pool={'metadata':{'uid':'pool'},'spec':{'template':{'spec':{'nodeClassRef':{'group':'karpenter.k8s.aws','kind':'EC2NodeClass','name':'cpu'}}}}}
        cls={'metadata':{'uid':'class','name':'cpu'},'spec':{'userData':'PRIVATE_USERDATA'}}
        calls=[]
        def read(*args):
            calls.append(args)
            if 'deployment'in args:return dep
            if 'hpa'in args:return h
            if 'scaledobject'in args:return so
            if 'nodepool'in args:return pool
            if 'ec2nodeclass'in args:return cls
            if 'application'in args:return {'status':{'sync':{'revision':'a'*40}}}
            return {'items':[]}
        return read,dep,h,calls

    def test_poll_hashes_sensitive_specs_and_handles_unobserved_status(self):
        read,dep,h,calls=self.fixture_reader();row=e.poll(read)
        self.assertNotIn('PRIVATE_',json.dumps(row));self.assertEqual(row['converter']['ready'],1)
        del dep['status']['readyReplicas'];self.assertEqual(e.poll(read)['converter']['ready'],0)
        dep['metadata']['generation']=2;self.assertIsNone(e.poll(read)['converter']['ready'])
        h['spec']['maxReplicas']=3
        with self.assertRaises(ValueError):e.poll(read)
        self.assertFalse(any('secret'in x or 'exec'in x for x in calls))

    def test_observer_streams_private_partial_evidence_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'run'
            with patch.object(e,'identity'),patch.object(e,'poll',side_effect=[sample(),ValueError('PRIVATE_ERROR'),sample()]),patch.object(e.time,'monotonic',side_effect=[0,0,0,30,30,60]),patch.object(e.time,'sleep'):
                r=e.observe(None,out,'trial1',60,30)
            self.assertEqual(r['sampleCount'],3);self.assertEqual(r['collectionStatus'],'partial')
            self.assertNotIn('PRIVATE_ERROR',(out/'samples.jsonl').read_text())
            self.assertEqual(out.stat().st_mode&0o777,0o700)
            self.assertEqual((out/'samples.jsonl').stat().st_mode&0o777,0o600)
            with patch.object(e,'identity'),self.assertRaises(FileExistsError):e.observe(None,out,'trial1',60)

    def test_aws_describe_only_for_observed_new_removed_worker(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);raw='\n'.join(json.dumps(x)for x in [sample(),sample(2,True),sample()]).encode()
            (root/'samples.jsonl').write_bytes(raw)
            meta={'kind':'v3-scaling-observation','clusterUid':e.CLUSTER_UID,'samplesSha256':hashlib.sha256(raw).hexdigest()};p=root/'result.json';p.write_text(json.dumps(meta))
            calls=[]
            def run(args,**kw):
                calls.append(args)
                data={'Account':'483175530259'}if 'sts'in args else {'Reservations':[{'Instances':[{'InstanceId':'i-'+'2'*17,'State':{'Name':'terminated'}}]}]}
                return SimpleNamespace(returncode=0,stdout=json.dumps(data))
            self.assertTrue(e.verify_node_release(p,run)['allNewWorkersTerminated'])
            self.assertEqual(len(calls),2);self.assertIn('describe-instances',calls[1]);self.assertNotIn('i-'+'1'*17,calls[1])
            self.assertIn('Name=tag:karpenter.sh/nodepool,Values=worker',calls[1])
            middle=sample(2,True);end=sample()
            extra={'uid':'node3','instanceId':'i-'+'3'*17}
            middle['nodes'].append(extra);end['nodes'].append(extra)
            partial_raw='\n'.join(json.dumps(x)for x in [sample(),middle,end]).encode()
            (root/'samples.jsonl').write_bytes(partial_raw);meta['samplesSha256']=hashlib.sha256(partial_raw).hexdigest();p.write_text(json.dumps(meta))
            partial=e.verify_node_release(p,run)
            self.assertTrue(partial['allRecordedCandidatesTerminated']);self.assertFalse(partial['allNewWorkersTerminated'])
            (root/'samples.jsonl').write_bytes(raw);meta['samplesSha256']=hashlib.sha256(raw).hexdigest();p.write_text(json.dumps(meta))
            def missing(args,**kw):return SimpleNamespace(returncode=0,stdout=json.dumps({'Account':'483175530259'}if 'sts'in args else {'Reservations':[]}))
            self.assertFalse(e.verify_node_release(p,missing)['allRecordedCandidatesTerminated'])
            with self.assertRaises(ValueError):e.verify_node_release(p,lambda *a,**k:SimpleNamespace(returncode=0,stdout='{"Account":"wrong"}'))
            (root/'samples.jsonl').write_bytes(raw+b' ')
            with self.assertRaises(ValueError):e.verify_node_release(p,run)

    def test_no_new_removed_node_never_calls_aws(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);raw=json.dumps(sample()).encode();(root/'samples.jsonl').write_bytes(raw)
            p=root/'result.json';p.write_text(json.dumps({'kind':'v3-scaling-observation','clusterUid':e.CLUSTER_UID,'samplesSha256':hashlib.sha256(raw).hexdigest()}))
            result=e.verify_node_release(p,lambda *a,**k:self.fail('AWS called'))
            self.assertIsNone(result['allRecordedCandidatesTerminated'])

    def test_interrupt_preserves_partial_samples_without_success_claim(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(e,'identity'),patch.object(e,'poll',side_effect=[sample(),KeyboardInterrupt]),patch.object(e.time,'monotonic',return_value=0),patch.object(e.time,'sleep'):
                r=e.observe(None,Path(d)/'run','trial1',60)
            self.assertEqual(r['stopReason'],'interrupted');self.assertEqual(r['sampleCount'],1)
            self.assertFalse(r['oneTwoOneObserved']);self.assertEqual(r['collectionStatus'],'partial')

    def test_local_manifest_is_stable_private_and_rejects_changed_or_duplicate_input(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);root.chmod(0o700);p=root/'a.jpg';p.write_bytes(b'\xff\xd8\xffsynthetic');selection=root/'selection.json';selection.write_text('["a.jpg"]')
            inspect=root/'inspection.json';inspect.write_text(json.dumps({'inspectionSource':'macOS ImageIO','files':[{'relativePath':'a.jpg','sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size,'hasGPS':True,'hasCaptureTime':True,'width':10,'height':20,'latitude':'MUST_NOT_EXPORT'}]}))
            original=p.read_bytes();data=e.input_manifest(root,selection,inspect,1);m=root/'manifest.json';e.save(m,data)
            summary=e.input_summary(m)
            self.assertEqual(summary['gpsPresentFiles'],1);self.assertNotIn('relativePath',summary)
            self.assertNotIn('latitude',json.dumps(data));self.assertEqual(p.read_bytes(),original)
            self.assertEqual(data['inputSetSha256'],e.input_manifest(root,selection,inspect,1)['inputSetSha256'])
            p.write_bytes(original+b'changed')
            with self.assertRaises(ValueError):e.input_manifest(root,selection,inspect,1)
            p.write_bytes(original);(root/'b.jpg').write_bytes(original);selection.write_text('["a.jpg","b.jpg"]')
            with self.assertRaises(ValueError):e.input_manifest(root,selection,expected=2)
            for name in ['../escape.jpg','/absolute.jpg']:
                selection.write_text(json.dumps([name]))
                with self.assertRaises(ValueError):e.input_manifest(root,selection,expected=1)
            (root/'link.jpg').symlink_to(p);selection.write_text('["link.jpg"]')
            with self.assertRaises(ValueError):e.input_manifest(root,selection,expected=1)

if __name__=='__main__':unittest.main()
