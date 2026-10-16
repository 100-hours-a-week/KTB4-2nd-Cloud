import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('release', ROOT / 'scripts/deploy-v2-staging-release.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
BASE = m.manifest.load(ROOT / m.MANIFEST)
REFS = m.image_refs(BASE)
BEFORE = {'images': REFS, 'revisions': {k: f'arn:aws:ecs:ap-northeast-2:483175530259:task-definition/{v}:3' for k,v in m.SERVICES.items()}}


def selected(changes=()):
    data = copy.deepcopy(BASE)
    for name in changes:
        data['components'][name]['digest'] = 'sha256:' + 'a'*64
    return {'pr': 240, 'cloudCommit': 'b'*40, 'ciRunId': 123, 'manifest': data}


class CandidateTests(unittest.TestCase):
    def exercise(self, run_change=None, v2='success', manifests=None):
        pr = {'merged': True, 'merge_commit_sha': 'b'*40,
              'base': {'ref':'main','repo':{'full_name':m.CLOUD}},
              'head': {'sha':'a'*40,'ref':'candidate','repo':{'full_name':m.CLOUD}}}
        run = {'id': 20, 'head_sha': 'a'*40, 'head_branch':'candidate', 'event':'pull_request',
               'path':'.github/workflows/cloud-pr-gate.yml', 'head_repository':{'full_name':m.CLOUD},
               'status':'completed','conclusion':'success', 'pull_requests':[]}
        older = run | {'id':10}
        if run_change:
            run.update(run_change)
        def api(path):
            if '/pulls/' in path:
                return pr
            if '/jobs?' in path:
                return {'jobs':[{'name':'V2 image combination smoke','conclusion':v2},
                                {'name':'cloud-ci-gate','conclusion':'success'}]}
            return {'workflow_runs':[older,run]}
        with patch.object(m,'api',side_effect=api), patch.object(m,'manifest_at',side_effect=manifests or [BASE,BASE,BASE]):
            return m.candidate('240')

    def test_empty_pr_association_is_allowed_only_with_exact_head_and_passed_jobs(self):
        self.assertEqual(self.exercise()['ciRunId'],20)

    def test_latest_failed_rerun_is_not_replaced_by_older_success(self):
        with self.assertRaisesRegex(ValueError,'Latest matching'):
            self.exercise(run_change={'conclusion':'failure'})

    def test_skipped_v2_gate_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'V2 image'):
            self.exercise(v2='skipped')

    def test_newer_main_manifest_supersedes_queued_delivery(self):
        self.assertIsNone(self.exercise(manifests=[BASE,selected(['frontend'])['manifest']]))

    def test_merge_manifest_must_match_tested_head(self):
        with self.assertRaisesRegex(ValueError,'PR candidate'):
            self.exercise(manifests=[BASE,BASE,selected(['frontend'])['manifest']])


class DeploymentTests(unittest.TestCase):
    def exercise(self, changes=(), last=True, gpu='stopped', fail=None, unknown=False):
        desired = selected(changes)
        after = {'images':m.image_refs(desired['manifest']), 'revisions':BEFORE['revisions']}
        prior = {'schemaVersion':1,'manifest':BASE,'runnerImage':REFS['ai-app'],'runnerVerifiedAt':'earlier'} if last else None
        writes, calls, power = [], [], []
        def aws(*args):
            if args[:2] == ('sts','get-caller-identity'):
                return {'Account':m.ai.ACCOUNT}
            power.append(args)
            return {}
        def write(name,data):
            writes.append((name,copy.deepcopy(data)))
        def service(name,action,value,directory):
            calls.append((name,action,value))
            if action == 'deploy' and name == fail:
                if unknown:
                    (directory/'ai-deploy.json').write_text(json.dumps({'failure':'Runner command status unknown'}))
                raise subprocess.CalledProcessError(1,['deploy',name])
        snaps = [BEFORE,BEFORE if fail else after]
        readiness = [REFS['ai-app'],m.image_refs(desired['manifest'])['ai-app']]
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ,{'RELEASE_REPORT_DIR':directory}), patch.object(sys,'argv',['release','--pr','240','--apply']), \
                 patch.object(m,'candidate',return_value=desired), patch.object(m,'snapshot',side_effect=snaps), \
                 patch.object(m,'manifest_at',return_value=desired['manifest']), patch.object(m,'get_state',side_effect=[prior,None]), \
                 patch.object(m,'aws',side_effect=aws), patch.object(m,'gpu_state',return_value=gpu), \
                 patch.object(m,'put_state',side_effect=write), patch.object(m,'ready_runner',side_effect=readiness) as ready, \
                 patch.object(m,'run_service',side_effect=service), patch('builtins.print'):
                if fail:
                    with self.assertRaises(subprocess.CalledProcessError):m.main()
                else:m.main()
                report=json.loads((Path(directory)/'release.json').read_text())
                return report,writes,calls,power,ready.call_count

    def test_fe_only_release_never_starts_or_checks_gpu(self):
        report,writes,calls,power,checks=self.exercise(['frontend'])
        self.assertEqual(report['status'],'healthy')
        self.assertEqual(power,[])
        self.assertEqual(checks,0)
        self.assertEqual([x[:2] for x in calls],[('frontend','deploy')])
        self.assertEqual([v for k,v in writes if k==m.LAST][0]['runnerVerifiedAt'],'earlier')

    def test_ai_release_starts_only_stopped_aws_gpu_then_requests_stop(self):
        report,writes,calls,power,checks=self.exercise(['ai-app'])
        self.assertEqual([x[:2] for x in power],[('ec2','start-instances'),('ec2','stop-instances')])
        self.assertEqual(report['gpuCleanup'],'stop_requested')
        self.assertEqual(checks,2)

    def test_originally_running_gpu_is_preserved(self):
        report,writes,calls,power,checks=self.exercise(['ai-app'],gpu='running')
        self.assertEqual(power,[])
        self.assertEqual(report['gpuCleanup'],'original_state_preserved')

    def test_initial_baseline_checks_runner_without_redeploying_unchanged_images(self):
        report,writes,calls,power,checks=self.exercise(last=False)
        self.assertEqual(calls,[])
        self.assertEqual(checks,2)
        self.assertTrue(any(k==m.LAST for k,v in writes))

    def test_partial_failure_restores_attempted_services_in_reverse_order(self):
        report,writes,calls,power,checks=self.exercise(['ai-app','backend','frontend'],fail='frontend')
        self.assertEqual(report['status'],'restored_previous')
        self.assertFalse(any(k==m.LAST for k,v in writes))
        self.assertEqual([x[:2] for x in calls],[('ai-app','deploy'),('backend','deploy'),('frontend','deploy'),
                                             ('frontend','rollback'),('backend','rollback'),('ai-app','rollback')])

    def test_unknown_ssm_result_never_competes_or_stops_recovery_gpu(self):
        report,writes,calls,power,checks=self.exercise(['ai-app'],fail='ai-app',unknown=True)
        self.assertEqual(report['status'],'recovery_required')
        self.assertEqual([x[:2] for x in calls],[('ai-app','deploy')])
        self.assertEqual([x[:2] for x in power],[('ec2','start-instances')])
        self.assertEqual(report['gpuCleanup'],'left_running_for_recovery')


if __name__=='__main__':unittest.main()
