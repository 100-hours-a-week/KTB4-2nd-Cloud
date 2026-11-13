"""Pool regressions and isolated CPU-credit bootstrap execution; no AWS/EKS access."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('pools', ROOT / 'scripts/check-v3-nodepools.py')
p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)


class PoolContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = p.platform.load_documents(p.render([p.FOUNDATION, p.FIXTURE]))

    def test_defaults_disabled_and_enabled_inventory(self):
        p.validate(p.platform.load_documents(p.render()), False)
        p.validate(p.platform.load_documents(p.render([p.FOUNDATION])), False)
        p.validate(self.docs, True)
        p.platform.schema_check(yaml.safe_dump_all(self.docs))

    def test_no_enable_without_credit_policy_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp)/'enable.yaml';file.write_text('enabled: true\n')
            with self.assertRaises(subprocess.CalledProcessError): p.render([p.FOUNDATION, file])

    def test_other_instance_types_and_higher_limits_are_rejected(self):
        for key, value in [('instanceType', 't3a.large'), ('cpuLimit', '8'), ('memoryLimit', '16Gi')]:
            with tempfile.TemporaryDirectory() as tmp:
                file = Path(tmp)/'bad.yaml';file.write_text(yaml.safe_dump({'pools': {'api': {key: value}}}))
                with self.subTest(key=key), self.assertRaises(subprocess.CalledProcessError): p.render([p.FOUNDATION,p.FIXTURE,file])

    def test_capacity_role_and_disruption_tampering_rejected(self):
        for kind in ('gpu', 'limits', 'replicas', 'taints', 'drift', 'expiry', 'class'):
            docs=copy.deepcopy(self.docs);pool=next(d for d in docs if d['kind']=='NodePool')
            if kind=='gpu':pool['metadata']['name']='gpu'
            if kind=='limits':pool['spec']['limits']['cpu']='8'
            if kind=='replicas':pool['spec']['replicas']=2
            if kind=='taints':pool['spec']['template']['spec']['taints']=[]
            if kind=='drift':pool['spec']['disruption']['budgets'][1]['nodes']='1'
            if kind=='expiry':pool['spec']['template']['spec']['expireAfter']='720h'
            if kind=='class':pool['spec']['template']['spec']['nodeClassRef']['name']='other'
            with self.subTest(kind=kind),self.assertRaises(ValueError):p.validate(docs,True)

    def test_class_network_iam_disk_and_bootstrap_tampering_rejected(self):
        for kind in ('ami','subnet','profile','public','disk','imds','userdata','extra'):
            docs=copy.deepcopy(self.docs);spec=next(d for d in docs if d['kind']=='EC2NodeClass')['spec']
            if kind=='ami':spec['amiSelectorTerms']=[{'alias':'al2023@latest'}]
            if kind=='subnet':spec['subnetSelectorTerms']=[{'tags':{'Name':'*'}}]
            if kind=='profile':spec['instanceProfile']='existing-system-role'
            if kind=='public':spec['associatePublicIPAddress']=True
            if kind=='disk':spec['blockDeviceMappings'][0]['ebs']['encrypted']=False
            if kind=='imds':spec['metadataOptions']['httpTokens']='optional'
            if kind=='userdata':spec['userData']=spec['userData'].replace('Requires=yeodam-cpu-credit-standard.service','Wants=yeodam-cpu-credit-standard.service')
            if kind=='extra':spec['role']='unreviewed'
            with self.subTest(kind=kind),self.assertRaises(ValueError):p.validate(docs,True)

    def test_nodeclass_validation_exception_is_named_and_empty_pool_only(self):
        policy=json.loads((p.CHART/'files/nodeclass-validation-policy.json').read_text())
        self.assertEqual(len(policy['Statement']),6)
        for statement in policy['Statement'][:-1]:
            conditions=statement['Condition']['StringEquals']
            prefix='aws:RequestTag/' if 'aws:RequestTag/karpenter.sh/nodepool' in conditions else 'aws:ResourceTag/'
            self.assertEqual(conditions[prefix+'karpenter.sh/nodepool'],'')
            self.assertEqual(conditions[prefix+'karpenter.k8s.aws/ec2nodeclass'],['yeodam-v3-app','yeodam-v3-worker'])
            self.assertEqual(conditions[prefix+'kubernetes.io/cluster/yeodam-v3-foundation'],'owned')
            actions=statement['Action'] if isinstance(statement['Action'],list) else [statement['Action']]
            self.assertNotIn('ec2:TerminateInstances',actions)
            self.assertNotIn('ec2:*',actions)
        profile=policy['Statement'][-1]
        self.assertEqual(profile['Action'],'iam:GetInstanceProfile')
        self.assertEqual(profile['Resource'],'arn:aws:iam::483175530259:instance-profile/yeodam-v3-foundation_7668371896316790615')
        cleanup=policy['Statement'][-2]
        self.assertEqual(cleanup['Action'],'ec2:DeleteLaunchTemplate')
        self.assertIn('launch-template/',cleanup['Resource'])

    def test_userdata_shell_and_systemd_gate_structure(self):
        c=next(d for d in self.docs if d['kind']=='EC2NodeClass')
        script=p.bootstrap_script(c['spec']['userData'])
        subprocess.run(['bash','-n'],input=script,text=True,check=True)
        unit=(p.CHART/'files/cpu-credit-standard.service').read_text()
        gate=(p.CHART/'files/kubelet-credit-gate.conf').read_text()
        self.assertIn('Before=kubelet.service',unit)
        self.assertIn('RemainAfterExit=yes',unit)
        self.assertIn('TimeoutStartSec=300',unit)
        self.assertIn('Requires=yeodam-cpu-credit-standard.service',gate)
        self.assertIn('After=yeodam-cpu-credit-standard.service',gate)


    def test_worker_burst_is_opt_in_and_does_not_change_api_or_old_class(self):
        docs=p.platform.load_documents(p.render([p.FOUNDATION,ROOT/'tests/ci/v3/nodepools-worker-burst.fixture.yaml']))
        p.validate(docs,True,True)
        p.platform.schema_check(yaml.safe_dump_all(docs))
        before={(d['kind'],d['metadata']['name']):d for d in self.docs}
        after={(d['kind'],d['metadata']['name']):d for d in docs}
        self.assertEqual(before[('EC2NodeClass','yeodam-v3-app')],after[('EC2NodeClass','yeodam-v3-app')])
        self.assertEqual(before[('NodePool','api')],after[('NodePool','api')])
        worker=copy.deepcopy(before[('NodePool','worker')])
        worker['spec']['template']['spec']['nodeClassRef']['name']='yeodam-v3-worker'
        self.assertEqual(worker,after[('NodePool','worker')])
        for seconds in [60,601,3600]:
            with tempfile.TemporaryDirectory() as tmp:
                file=Path(tmp)/'override.yaml';file.write_text(yaml.safe_dump({'workerStartupBurst':{'seconds':seconds}}))
                with self.assertRaises(subprocess.CalledProcessError):p.render([p.FOUNDATION,p.FIXTURE,file])

    def test_worker_timer_and_shell_are_valid_and_cannot_renew_from_boot(self):
        for name in ['worker-credit.sh','cpu-credit-standard.sh']:
            subprocess.run(['bash','-n',str(p.CHART/'files'/name)],check=True)
        timer=(p.CHART/'files/worker-credit-restore.timer').read_text()
        self.assertIn('OnBootSec=600s',timer)
        self.assertNotIn('OnActiveSec=',timer)
        unit=(p.CHART/'files/worker-credit-restore.service').read_text()
        self.assertIn('Restart=on-failure',unit)
        self.assertIn('RestartSec=15s',unit)


class CreditBootstrap(unittest.TestCase):
    def run_guard(self, case):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);binpath=directory/'bin';binpath.mkdir()
            # Only the test copy changes PATH; the production script has no test switch.
            source=(p.CHART/'files/cpu-credit-standard.sh').read_text().replace(
                'export PATH=/usr/local/bin:/usr/bin:/bin',f'export PATH={binpath}:/usr/bin:/bin')
            script=directory/'guard.sh';script.write_text(source)
            self.write(binpath/'curl',r'''import os,sys
s=' '.join(sys.argv)
case=os.environ['CREDIT_CASE']
if case=='metadata-failure':sys.exit(22)
if '/api/token' in s:print('synthetic-never-log-token')
elif 'instance-id' in s:print('bad-id' if case=='bad-id' else 'i-0123456789abcdef0')
elif 'placement/region' in s:print('us-east-1' if case=='bad-region' else 'ap-northeast-2')
elif 'instance-type' in s:print('t3a.large' if case=='bad-type' else 't3a.medium')
else:sys.exit(90)
''')
            self.write(binpath/'aws',r'''import os,sys,json
from pathlib import Path
args=sys.argv[1:];case=os.environ['CREDIT_CASE'];root=Path(os.environ['CREDIT_TMP'])
with (root/'calls').open('a') as f:f.write(json.dumps(args)+'\n')
assert '--region' in args and args[args.index('--region')+1]=='ap-northeast-2'
assert '--cli-read-timeout' in args and '--cli-connect-timeout' in args
if args[1]=='modify-instance-credit-specification':
 assert args[args.index('--instance-credit-specifications')+1]=='InstanceId=i-0123456789abcdef0,CpuCredits=standard'
 if case=='denied':print('synthetic-private-api-error',file=sys.stderr);sys.exit(1)
 print('{"Unsuccessful":[{"Error":{"Code":"UnauthorizedOperation"}}]}' if case=='unsuccessful-response' else '{}')
elif args[1]=='describe-instance-credit-specifications':
 assert args[args.index('--instance-ids')+1]=='i-0123456789abcdef0'
 file=root/'attempts';n=int(file.read_text())+1 if file.exists() else 1;file.write_text(str(n))
 if case=='denied':sys.exit(1)
 print('standard' if case=='success' or (case=='retry-success' and n>=2) else 'unlimited')
else:raise AssertionError('Unexpected API mutation')
''')
            self.write(binpath/'sleep','pass')
            result=subprocess.run(['bash',str(script)],capture_output=True,text=True,
                env={**os.environ,'CREDIT_CASE':case,'CREDIT_TMP':tmp},timeout=15)
            calls=[json.loads(x) for x in (directory/'calls').read_text().splitlines()] if (directory/'calls').exists() else []
            return result,calls

    def write(self,path,source):
        path.write_text('#!'+sys.executable+'\n'+source);path.chmod(0o755)

    def test_only_verified_standard_succeeds_and_targets_own_id(self):
        for case in ('success','retry-success'):
            result,calls=self.run_guard(case)
            with self.subTest(case=case):
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIn('mode verified: standard',result.stdout)
                self.assertEqual(len(calls),2 if case=='success' else 4)
                self.assertNotIn('synthetic-never-log-token',result.stdout+result.stderr)

    def test_unlimited_denied_or_unsuccessful_modify_never_passes_gate(self):
        for case in ('unlimited','denied','unsuccessful-response'):
            result,calls=self.run_guard(case)
            with self.subTest(case=case):
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(len(calls),24)
                self.assertIn('kubelet start denied',result.stderr)
                self.assertNotIn('synthetic-private-api-error',result.stdout+result.stderr)

    def test_bad_metadata_never_attempts_ec2_change(self):
        for case in ('bad-id','bad-region','bad-type','metadata-failure'):
            result,calls=self.run_guard(case)
            with self.subTest(case=case):
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(calls,[])
                self.assertNotIn('synthetic-never-log-token',result.stdout+result.stderr)


class WorkerStartupBurst(unittest.TestCase):
    def harness(self,case,operations):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);binpath=root/'bin';binpath.mkdir()
            source=(p.CHART/'files/worker-credit.sh').read_text()
            source=source.replace('export PATH=/usr/local/bin:/usr/bin:/bin',f'export PATH={binpath}:/usr/bin:/bin')
            source=source.replace('state=/var/lib/yeodam/worker-credit',f'state={root}/state')
            source=source.replace('standard=/usr/local/sbin/yeodam-cpu-credit-standard',f'standard={binpath}/standard')
            source=source.replace('/run/lock/yeodam-worker-credit.lock',str(root/'lock'))
            source=source.replace("boot=$(awk '$1==\"btime\"{print $2}' /proc/stat)",'boot=1000')
            script=root/'worker.sh';script.write_text(source)
            helpers={
'curl':r"""import sys
s=' '.join(sys.argv)
if '/api/token' in s:print('synthetic-never-log-token')
elif 'instance-id' in s:print('i-0123456789abcdef0')
elif 'placement/region' in s:print('ap-northeast-2')
elif 'instance-type' in s:print('t3a.medium')
else:sys.exit(90)
""",
'date':r"""import os
print(1700 if os.environ['CASE']=='expired' else 1010)
""",
'flock':'pass',
'sync':'pass',
'sleep':'pass',
'standard':r"""import os,sys
from pathlib import Path
r=Path(os.environ['TEST_ROOT'])
with (r/'calls').open('a') as f:f.write('standard\n')
if os.environ['CASE']=='restore-denied':sys.exit(1)
(r/'mode').write_text('standard')
""",
'systemctl':r"""import os,sys
from pathlib import Path
r=Path(os.environ['TEST_ROOT']);args=sys.argv[1:]
with (r/'calls').open('a') as f:f.write('systemctl '+' '.join(args)+'\n')
if args[0]=='enable' and os.environ['CASE']=='timer-failure':sys.exit(1)
if args[0]=='is-active' and os.environ['CASE']=='timer-inactive':sys.exit(1)
if args[0]=='is-enabled' and os.environ['CASE']=='timer-disabled':sys.exit(1)
""",
'aws':r"""import os,sys
from pathlib import Path
r=Path(os.environ['TEST_ROOT']);args=sys.argv[1:]
assert args[args.index('--region')+1]=='ap-northeast-2'
assert '--cli-connect-timeout' in args and '--cli-read-timeout' in args
if args[1]=='modify-instance-credit-specification':
 assert args[args.index('--instance-credit-specifications')+1]=='InstanceId=i-0123456789abcdef0,CpuCredits=unlimited'
 with (r/'calls').open('a') as f:f.write('modify own unlimited\n')
 if os.environ['CASE']=='unverified':print('{"Unsuccessful":[{}]}')
 else:(r/'mode').write_text('unlimited');print('{}')
elif args[1]=='describe-instance-credit-specifications':
 assert args[args.index('--instance-ids')+1]=='i-0123456789abcdef0'
 print((r/'mode').read_text() if (r/'mode').exists() else 'standard')
else:raise AssertionError('Unexpected AWS API')
"""}
            for name,text in helpers.items():
                helper=binpath/name;helper.write_text('#!'+sys.executable+'\n'+text);helper.chmod(0o755)
            results=[]
            for op in operations:
                results.append(subprocess.run(['bash',str(script),op],text=True,capture_output=True,env={**os.environ,'CASE':case,'TEST_ROOT':str(root)},timeout=10))
            calls=(root/'calls').read_text().splitlines()if (root/'calls').exists()else []
            return results,calls,(root/'mode').read_text()if (root/'mode').exists()else None,(root/'state'/'attempted').read_text()if (root/'state'/'attempted').exists()else None

    def test_timer_is_armed_before_unlimited_and_restore_really_returns_standard(self):
        results,calls,mode,deadline=self.harness('success',['prepare','restore'])
        self.assertEqual([r.returncode for r in results],[0,0])
        self.assertLess(next(i for i,x in enumerate(calls)if 'is-active' in x),calls.index('modify own unlimited'))
        self.assertEqual(mode,'standard');self.assertEqual(deadline.strip(),'1600')
        self.assertNotIn('synthetic-never-log-token',''.join(x.stdout+x.stderr for x in results))

    def test_reentry_or_reboot_never_reopens_burst(self):
        results,calls,mode,_=self.harness('success',['prepare','prepare'])
        self.assertEqual([r.returncode for r in results],[0,0])
        self.assertEqual(calls.count('modify own unlimited'),1)
        self.assertEqual(mode,'standard')

    def test_expired_boot_window_never_enables_chargeable_cpu(self):
        results,calls,mode,deadline=self.harness('expired',['prepare'])
        self.assertEqual(results[0].returncode,0)
        self.assertNotIn('modify own unlimited',calls)
        self.assertEqual(mode,'standard');self.assertIsNone(deadline)

    def test_timer_failure_does_not_enable_unlimited(self):
        for case in ['timer-failure','timer-inactive','timer-disabled']:
            results,calls,mode,_=self.harness(case,['prepare'])
            self.assertNotEqual(results[0].returncode,0)
            self.assertNotIn('modify own unlimited',calls)

    def test_unverified_change_blocks_join_and_starts_independent_restore(self):
        results,calls,mode,_=self.harness('unverified',['prepare'])
        self.assertNotEqual(results[0].returncode,0)
        self.assertEqual(calls.count('modify own unlimited'),3)
        self.assertIn('systemctl start --no-block yeodam-worker-credit-restore.service',calls)
        self.assertIn('kubelet start denied',results[0].stderr)

    def test_failed_restore_does_not_claim_success_or_disable_timer(self):
        results,calls,mode,_=self.harness('restore-denied',['restore'])
        self.assertNotEqual(results[0].returncode,0)
        self.assertIn('restore FAILED',results[0].stderr)
        self.assertFalse(any(x.startswith('systemctl stop')for x in calls))
