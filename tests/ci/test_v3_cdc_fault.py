import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import v3_cdc_fault as f
import v3_chaos_install as install


def fixture():
    return {'version': 1, 'capturedAt': f.now(), 'cluster': f.CLUSTER,
            'kubeSystemUid': '11111111-1111-1111-1111-111111111111',
            'approvedRevision': 'a' * 40, 'serviceIp': '172.20.20.10',
            'controllerNode': 'ip-10-40-12-10.ap-northeast-2.compute.internal',
            'controllerNodeUid': '55555555-5555-5555-5555-555555555555',
            'controllerRole': 'system', 'workerControllerApproved': False,
            'podCapacity': {name: {'limit': 17, 'used': 16, 'ownPods': 0} for name in (
                'ip-10-40-12-10.ap-northeast-2.compute.internal', 'ip-10-40-12-11.ap-northeast-2.compute.internal')},
            'connect': {'name': f.CONNECT + '-connect-0', 'uid': '22222222-2222-2222-2222-222222222222',
                        'node': 'ip-10-40-12-11.ap-northeast-2.compute.internal', 'ip': '10.40.12.100',
                        'ownerKind': 'StrimziPodSet', 'ownerName': f.CONNECT + '-connect'},
            'mysql': {'name': 'mysql-0', 'uid': '33333333-3333-3333-3333-333333333333',
                      'node': 'ip-10-40-40-10.ap-northeast-2.compute.internal', 'ip': '10.40.40.100',
                      'ownerKind': 'StatefulSet', 'ownerName': 'mysql'}}


class FakeKube:
    def __init__(self, plan, interrupt=False):
        self.plan, self.interrupt, self.actions = plan, interrupt, []
        self.obj = None

    def get(self, kind, ns=None, name=None):
        if kind == 'networkchaos':
            return copy.deepcopy(self.obj) if name else {'items': []}
        if kind == 'daemonset':
            return {'spec': {'template': {'spec': {'nodeSelector': {
                'yeodam.io/role':'system', 'kubernetes.io/hostname': self.plan['snapshot']['connect']['node']}}}},
                    'status': {'numberReady': 1, 'desiredNumberScheduled': 1}}
        if kind == 'kafkaconnector':
            return {'metadata': {'generation': 1}, 'spec': {'state': 'running'},
                    'status': {'observedGeneration': 1, 'connectorStatus': {
                        'connector': {'state': 'RUNNING'}, 'tasks': [{'id': 0, 'state': 'RUNNING'}]}}}
        raise AssertionError(kind)

    def call(self, args, source=None, timeout=20):
        self.actions.append(args)
        if args[0] == 'create':
            self.obj = json.loads(source)
            self.obj['metadata'].update(uid='44444444-4444-4444-4444-444444444444', resourceVersion='7', creationTimestamp=f.now())
            self.obj['status'] = {'conditions': [{'type': 'AllInjected', 'status': 'True'}]}
            return json.dumps(self.obj)
        if 'patch' in args:
            value = json.loads(args[-1])[-1]['value']
            self.obj['metadata']['annotations'][f.PAUSE] = value
            if self.interrupt and value == 'false':
                raise KeyboardInterrupt()
        return ''


class FaultTests(unittest.TestCase):
    def test_capacity_blocks_before_render_and_counts_installed_daemon_slot(self):
        s = fixture(); c = s['podCapacity'][s['connect']['node']]; c['used'] = 17
        with self.assertRaises(f.GuardError): install.values(s)
        c['ownPods'] = 1
        install.values(s)
        s['podCapacity'][s['controllerNode']]['used'] = 17
        with self.assertRaises(f.GuardError): install.values(s)

    def test_worker_requires_explicit_opt_in_and_narrow_taint(self):
        s = fixture(); name = s['controllerNode']
        node = {'metadata': {'name': name, 'uid': s['controllerNodeUid'], 'labels': {
            'yeodam.io/role': 'worker', 'node.kubernetes.io/instance-type': 't3a.medium'}},
            'spec': {'taints': [{'key': 'yeodam.io/role', 'value': 'worker', 'effect': 'NoSchedule'}]},
            'status': {'conditions': [{'type': 'Ready', 'status': 'True'}], 'allocatable': {'pods': '17'},
                       'nodeInfo': {'operatingSystem': 'linux', 'architecture': 'amd64'}}}
        connect = copy.deepcopy(node); connect['metadata']['name'] = s['connect']['node']
        connect['metadata']['labels']['yeodam.io/role'] = 'system'; connect['spec'] = {}
        nodes = [node, connect]
        for host, opt in ((None, False), (None, True), (name, False)):
            with self.subTest(host=host, opt=opt), self.assertRaises(f.GuardError):
                f.controller_placement(nodes, [], s['connect']['node'], host, opt)
        s.update(f.controller_placement(nodes, [], s['connect']['node'], name, True))
        v = install.values(s)
        self.assertEqual(v['controllerManager']['nodeSelector']['yeodam.io/role'], 'worker')
        self.assertEqual(v['controllerManager']['tolerations'], [{'key': 'yeodam.io/role',
            'operator': 'Equal', 'value': 'worker', 'effect': 'NoSchedule'}])
        self.assertEqual(v['chaosDaemon']['nodeSelector']['yeodam.io/role'], 'system')
        for role in ('api', 'data', 'gpu'):
            node['metadata']['labels']['yeodam.io/role'] = role
            with self.assertRaises(f.GuardError):
                f.controller_placement(nodes, [], s['connect']['node'], name, True)

    def test_null_exec_environment_uses_controlled_profile(self):
        config = {'users': [{'user': {'exec': {'command': 'aws', 'env': None, 'args': [
            'eks', 'get-token', '--cluster-name', f.CLUSTER, '--region', 'ap-northeast-2']}}}],
                  'clusters': [{'cluster': {}}]}
        k = f.Kube('/unused')
        with patch.object(k, 'call', return_value=json.dumps(config)), \
                patch.object(k, 'get', return_value={'metadata': {'uid': fixture()['kubeSystemUid']}}), \
                patch.object(f.subprocess, 'run') as run:
            run.return_value.returncode = 0; run.return_value.stdout = '{"Account":"483175530259"}'
            self.assertEqual(k.identity(), fixture()['kubeSystemUid'])

    def test_partition_exact_source_and_port_no_mysql_target_injection(self):
        p = f.prepare(fixture(), 'cdc-first', 60)
        spec = p['fault']['spec']
        self.assertEqual(spec['selector']['pods'], {'kafka': [fixture()['connect']['name']]})
        self.assertEqual(spec['externalTargets'], ['10.40.40.100:3306', '172.20.20.10:3306'])
        self.assertEqual(spec['direction'], 'to')
        self.assertNotIn('target', spec)
        self.assertEqual(p['fault']['metadata']['annotations'][f.PAUSE], 'true')

    def test_stale_future_wrong_cluster_or_public_destination_blocked(self):
        for key, value in [('capturedAt', '2020-01-01T00:00:00+00:00'),
                           ('capturedAt', '2099-01-01T00:00:00+00:00'), ('cluster', 'production'),
                           ('serviceIp', '8.8.8.8')]:
            s = fixture(); s[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                f.prepare(s, 'cdc-first', 60)
        s = fixture(); s['mysql']['ip'] = '10.0.0.1'
        with self.assertRaises(ValueError): f.prepare(s, 'cdc-first', 60)

    def test_invalid_duration_and_name_blocked(self):
        for seconds in (0, 14, 121, 3600, True):
            with self.assertRaises(ValueError): f.prepare(fixture(), 'cdc-first', seconds)
        with self.assertRaises(ValueError): f.prepare(fixture(), '*', 60)

    def test_tampered_scope_not_authorized_by_rehashed_plan(self):
        p = f.prepare(fixture(), 'cdc-first', 60)
        p['fault']['spec']['externalTargets'] = ['10.40.0.0/16']
        p['planSha256'] = f.sha({k:v for k,v in p.items() if k != 'planSha256'})
        with self.assertRaises(ValueError): f.verify_plan(p, p['planSha256'])

    def test_live_pod_replacement_or_primary_switch_requires_new_plan(self):
        p = f.prepare(fixture(), 'cdc-first', 60)
        for role in ('connect', 'mysql'):
            s = fixture(); s[role]['uid'] = 'f' * 8 + '-1111-1111-1111-111111111111'
            with self.assertRaises(ValueError): f.compare_live(p, s)

    def test_foreign_fault_cannot_be_recovered(self):
        p = f.prepare(fixture(), 'cdc-first', 60)
        obj = copy.deepcopy(p['fault']); obj['metadata']['uid'] = 'foreign'
        with self.assertRaises(ValueError): f.owned_fault(obj, p, 'expected')

    def test_success_requests_pause_and_does_not_claim_business_recovery(self):
        p = f.prepare(fixture(), 'cdc-first', 15); k = FakeKube(p)
        with tempfile.TemporaryDirectory() as t, patch.object(f, 'snapshot', return_value=p['snapshot']), \
                patch.object(f.time, 'monotonic', side_effect=[0, 1, 16]), patch.object(f.time, 'sleep'):
            result = f.execute(k, p, p['planSha256'], Path(t)/'receipt.json')
            self.assertTrue(result['injectionConfirmed'])
            self.assertFalse(result['businessRecoveryVerified'])
            self.assertTrue(result['recovery']['controllerReportedRecovered'])
            self.assertFalse(result['recovery']['dataPathRecoveryVerified'])
            self.assertEqual(k.obj['metadata']['annotations'][f.PAUSE], 'true')
            self.assertTrue(any('wait' in a for a in k.actions))
            self.assertFalse(any('delete' in a for a in k.actions))

    def test_interrupt_after_activation_still_recovers_owned_fault(self):
        p = f.prepare(fixture(), 'cdc-first', 15); k = FakeKube(p, interrupt=True)
        with tempfile.TemporaryDirectory() as t, patch.object(f, 'snapshot', return_value=p['snapshot']):
            with self.assertRaises(KeyboardInterrupt):
                f.execute(k, p, p['planSha256'], Path(t)/'receipt.json')
            result = json.loads((Path(t)/'receipt.json.finished.json').read_text())
            self.assertTrue(result['recovery']['controllerReportedRecovered'])
            self.assertEqual(k.obj['metadata']['annotations'][f.PAUSE], 'true')

    def test_no_arbitrary_sql_or_large_user_scope(self):
        for ids in ([], [1]*2, list(range(1,12)), ['1 OR 1=1'], [True], [-1]):
            with self.assertRaises(ValueError): f.evidence_sql(ids)
        sql = f.evidence_sql([123,124])
        self.assertIn('START TRANSACTION READ ONLY', sql)
        self.assertIn('MAX_EXECUTION_TIME=3000', sql)
        self.assertEqual(sql.count('LIMIT 201'), 4)
        self.assertEqual(sql.count('trip_id IN (123,124)'), 5)
        for forbidden in ('payload', 'user_id', 'SELECT *', 'UPDATE ', 'DELETE ', 'INSERT '):
            self.assertNotIn(forbidden, sql)

    def test_missing_receipts_and_raw_duplicates_remain_unknown(self):
        b = {'tripIds':[1], 'approvedRevision':'a'*40, 'coverage':'bounded-trial-only',
             'rows':[{'section':'creation_outbox','outboxId':'one','status':'PENDING'}]}
        a = dict(b, rows=[])
        result = f.compare_evidence(b,a)
        self.assertEqual(result['unconfirmedOutboxIds'], ['one'])
        self.assertIsNone(result['rawDeliveryCount'])
        self.assertIsNone(result['duplicateBusinessEffects'])
        self.assertFalse(result['businessRecoveryVerified'])
        self.assertEqual(result['verdict'], 'not_assessed')
        with self.assertRaises(ValueError): f.compare_evidence(b, dict(a, coverage='truncated'))

    def test_empty_records_not_a_success(self):
        b = {'tripIds':[1], 'approvedRevision':'a'*40, 'coverage':'bounded-trial-only', 'rows':[]}
        self.assertEqual(f.compare_evidence(b,b)['verdict'], 'no_committed_trial_evidence')

    def test_private_outputs_never_overwrite(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)/'out.json'; f.private_write(p, {'ok':True})
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError): f.private_write(p,{})
            (Path(t)/'link').symlink_to(p)
            with self.assertRaises(ValueError): f.private_write(Path(t)/'link',{})

    def test_source_values_are_unselected_and_small(self):
        v = install.yaml.safe_load((f.CHART/'values.yaml').read_text())
        self.assertFalse(v['clusterScoped'])
        self.assertEqual(v['controllerManager']['targetNamespace'], 'kafka')
        self.assertEqual(v['controllerManager']['nodeSelector'], {'yeodam.io/chaos-reviewed-host':'not-selected'})
        for key in ('dashboard','dnsServer','prometheus'): self.assertFalse(v[key]['create'])
        self.assertTrue(v['chaosDaemon']['mtls']['enabled'])

    @unittest.skipUnless(os.environ.get('V3_HELM'), 'Set pinned V3_HELM for actual Helm/schema validation')
    def test_actual_upstream_helm_render_and_official_fault_schema(self):
        s = fixture()
        s.update(controllerRole='worker', workerControllerApproved=True)
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)/'values.json'; f.private_write(p,install.values(s))
            docs = install.render(s,os.environ['V3_HELM'],p)
            self.assertEqual(sum(x['kind']=='CustomResourceDefinition' for x in docs),2)
            self.assertFalse(any('dashboard' in x['metadata']['name'] for x in docs))
            target = next(x for x in docs if x['kind']=='ClusterRole' and x['metadata']['name'].endswith('target-namespace'))
            self.assertFalse(any('secrets' in r['resources'] for r in target['rules']))
            # Compile the pinned CRD's OpenAPI schema with the repository's kubeconform binary.
            crd = next(x for x in docs if x['kind']=='CustomResourceDefinition' and x['metadata']['name']=='networkchaos.chaos-mesh.org')
            schema = next(v['schema']['openAPIV3Schema'] for v in crd['spec']['versions'] if v['name']=='v1alpha1')
            schema_path = Path(t)/'NetworkChaos.json'; schema_path.write_text(json.dumps(schema))
            fault = Path(t)/'fault.json'; fault.write_text(json.dumps(f.prepare(s,'cdc-schema',60)['fault']))
            import subprocess
            subprocess.run([os.environ['V3_KUBECONFORM'],'-strict','-summary','-schema-location',str(schema_path),str(fault)],check=True)
            builtins = Path(t)/'install.yaml'
            builtins.write_text(install.yaml.safe_dump_all([x for x in docs if x['kind'] != 'CustomResourceDefinition']))
            subprocess.run([os.environ['V3_KUBECONFORM'],'-strict','-summary','-kubernetes-version','1.36.0',
                            '-schema-location',str(Path(os.environ['V3_SCHEMA_DIR'])/'{{.ResourceKind}}.json'),str(builtins)],check=True)


if __name__ == '__main__': unittest.main()
