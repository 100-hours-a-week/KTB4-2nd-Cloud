import copy
import importlib.util
import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('prepare_release', ROOT / 'scripts/prepare-production-release.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
BASE = m.checks.manifest.load(ROOT / m.checks.MANIFEST)
POLICY = {'schemaVersion': 1, 'target': 'kubernetes', 'deploymentEnabled': False}


class ReleasePreparationTests(unittest.TestCase):
    def exercise(self, pr_change=None, files=None, ci_change=None, job_result='success',
                 manifests=None, publisher_change=None, publisher_missing=False, commit=None):
        pr = {'number': 506, 'merged': True, 'merged_at': '2026-11-04T00:00:00Z',
              'merge_commit_sha': 'b' * 40, 'changed_files': 1,
              'base': {'ref': 'main', 'repo': {'full_name': m.checks.CLOUD}},
              'head': {'sha': 'a' * 40, 'ref': 'automation/v2-release-manifest',
                       'repo': {'full_name': m.checks.CLOUD}}}
        pr.update(pr_change or {})
        calls = []

        def api(path):
            calls.append(path)
            if '/commits/' in path and '/pulls?' in path:
                return [pr]
            if '/files?' in path:
                return files if files is not None else [{'filename': m.checks.MANIFEST, 'status': 'modified'}]
            if '/pulls/' in path:
                return pr
            if '/jobs?' in path:
                return {'jobs': [{'name': 'V2 image combination smoke', 'conclusion': job_result},
                                 {'name': 'cloud-ci-gate', 'conclusion': 'success'}]}
            if '/actions/workflows/cloud-pr-gate.yml/' in path:
                run = {'id': 800, 'head_sha': 'a' * 40, 'head_branch': pr['head']['ref'],
                       'event': 'pull_request', 'path': '.github/workflows/cloud-pr-gate.yml',
                       'head_repository': {'full_name': m.checks.CLOUD},
                       'status': 'completed', 'conclusion': 'success'}
                return {'workflow_runs': [run, run | {'id': 801} | (ci_change or {})]}
            for name, entry in BASE['components'].items():
                if path.startswith('repos/' + entry['sourceRepository'] + '/actions/workflows/'):
                    run = {'id': 900, 'head_sha': entry['sourceSha'], 'head_branch': 'main',
                           'path': '.github/workflows/' + m.PUBLISHERS[name], 'event': 'push',
                           'repository': {'full_name': entry['sourceRepository']},
                           'head_repository': {'full_name': entry['sourceRepository']},
                           'status': 'completed', 'conclusion': 'success'}
                    return {'workflow_runs': [] if publisher_missing else [run, run | {'id': 901} | (publisher_change or {})]}
            raise AssertionError('Unexpected evidence API: ' + path)

        with patch.object(m, 'read_policy', return_value=POLICY), \
                patch.object(m.checks, 'api', side_effect=api), \
                patch.object(m.checks, 'manifest_at', side_effect=manifests or [BASE, BASE, BASE]), \
                patch.object(m.checks, 'aws', side_effect=AssertionError('AWS is forbidden')):
            result = m.prepare(pr_number=None if commit else '506', commit=commit)
        self.assertFalse(any('staging' in p or 'artifacts' in p for p in calls))
        return result

    def test_merged_verified_candidate_records_hold_without_staging_or_aws(self):
        report = self.exercise()
        self.assertEqual(report['status'], 'disabled')
        self.assertTrue(report['candidateVerified'])
        self.assertFalse(report['deployed'])
        self.assertFalse(report['deploymentEnabled'])
        self.assertFalse(report['executorReady'])
        self.assertEqual(report['target'], 'kubernetes')
        self.assertEqual(report['images'], m.checks.image_refs(BASE))
        self.assertEqual(report['ciRunId'], 801)

    def test_push_requires_exact_merge_commit(self):
        self.assertEqual(self.exercise(commit='b' * 40)['cloudCommit'], 'b' * 40)
        with self.assertRaises(ValueError):
            self.exercise(commit='c' * 40)

    def test_unmerged_foreign_or_wrong_base_pr_rejected(self):
        for change in [{'merged': False}, {'merge_commit_sha': 'main'},
                       {'base': {'ref': 'develop', 'repo': {'full_name': m.checks.CLOUD}}},
                       {'head': {'repo': {'full_name': 'other/fork'}}}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.exercise(pr_change=change)

    def test_code_policy_and_renamed_manifest_prs_cannot_approve_release(self):
        for files in [[], [{'filename': 'deploy/production/release-policy.json', 'status': 'modified'}],
                      [{'filename': m.checks.MANIFEST, 'status': 'renamed'}],
                      [{'filename': m.checks.MANIFEST, 'status': 'modified'}, {'filename': 'scripts/other.py'}]]:
            with self.subTest(files=files), self.assertRaisesRegex(ValueError, 'only the image manifest'):
                self.exercise(files=files)
        with self.assertRaises(ValueError):
            self.exercise(pr_change={'changed_files': 2})

    def test_latest_failed_or_unfinished_gate_never_falls_back_to_older_success(self):
        for change in [{'conclusion': 'failure'}, {'status': 'in_progress', 'conclusion': None}]:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'Latest matching'):
                self.exercise(ci_change=change)

    def test_skipped_required_smoke_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Required CI job'):
            self.exercise(job_result='skipped')

    def test_manifest_different_from_tested_pr_head_is_rejected(self):
        changed = copy.deepcopy(BASE)
        changed['components']['backend']['digest'] = 'sha256:' + 'a' * 64
        with self.assertRaisesRegex(ValueError, 'PR candidate'):
            self.exercise(manifests=[BASE, BASE, changed])

    def test_superseded_manifest_is_not_prepared_for_deployment(self):
        changed = copy.deepcopy(BASE)
        changed['components']['backend']['digest'] = 'sha256:' + 'a' * 64
        report = self.exercise(manifests=[BASE, changed])
        self.assertEqual(report['status'], 'superseded')
        self.assertFalse(report['deployed'])
        self.assertNotIn('candidateVerified', report)

    def test_missing_main_publisher_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'No matching main image publisher'):
            self.exercise(publisher_missing=True)

    def test_latest_failed_or_unfinished_publisher_does_not_use_older_success(self):
        for change in [{'conclusion': 'failure'}, {'status': 'in_progress', 'conclusion': None}]:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'Latest matching image publisher'):
                self.exercise(publisher_change=change)

    def test_foreign_branch_sha_or_workflow_publisher_is_not_evidence(self):
        for change in [{'head_branch': 'other'}, {'head_sha': 'f' * 40},
                       {'path': '.github/workflows/other.yml'},
                       {'head_repository': {'full_name': 'other/fork'}}]:
            # The valid older publisher remains legitimate; remove it to verify
            # that the modified record alone cannot establish provenance.
            with self.subTest(change=change):
                data = copy.deepcopy(BASE)
                name = next(iter(data['components']))
                entry = data['components'][name]
                run = {'id': 1, 'head_sha': entry['sourceSha'], 'head_branch': 'main',
                       'path': '.github/workflows/' + m.PUBLISHERS[name], 'event': 'push',
                       'repository': {'full_name': entry['sourceRepository']},
                       'head_repository': {'full_name': entry['sourceRepository']},
                       'status': 'completed', 'conclusion': 'success'} | change
                with patch.object(m.checks, 'api', return_value={'workflow_runs': [run]}), \
                        self.assertRaisesRegex(ValueError, 'No matching main image publisher'):
                    m.verify_publishers(data)


class ExecutionBoundaryTests(unittest.TestCase):
    def policy(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'policy.json'
            path.write_text(json.dumps(data))
            with patch.object(m, 'POLICY', path):
                return m.read_policy()

    def test_checked_in_policy_is_disabled_kubernetes(self):
        self.assertEqual(m.read_policy(), POLICY)

    def test_enabled_unsupported_targets_and_malformed_switches_are_rejected(self):
        for change in [{'deploymentEnabled': True}, {'deploymentEnabled': 'false'},
                       {'target': 'ecs'}, {'target': 'staging'}, {'schemaVersion': True}, {'extra': True}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.policy(POLICY | change)

    def test_apply_never_reaches_github_or_aws(self):
        with patch.object(m.checks, 'api') as api, patch.object(m.checks, 'aws') as aws, \
                self.assertRaisesRegex(ValueError, 'deployment is disabled'):
            m.prepare(pr_number='506', apply=True)
        api.assert_not_called()
        aws.assert_not_called()

    def test_manual_cli_apply_emits_rejected_report_and_nonzero(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(m.checks, 'api') as api:
            path = Path(directory) / 'report.json'
            self.assertEqual(m.main(['--pr', '506', '--apply', '--report', str(path)]), 1)
            self.assertEqual(json.loads(path.read_text())['status'], 'rejected')
            self.assertFalse(json.loads(path.read_text())['deployed'])
            api.assert_not_called()

    def test_missing_policy_cannot_default_to_enabled(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(m, 'POLICY', Path(directory) / 'missing.json'), \
                patch.object(m.checks, 'api') as api:
            with self.assertRaises(OSError):
                m.prepare(pr_number='506')
            api.assert_not_called()

    def test_failed_github_lookup_is_recorded_without_claiming_deployment(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(m.checks, 'api', side_effect=subprocess.CalledProcessError(1, ['gh', 'api'])):
            path = Path(directory) / 'report.json'
            self.assertEqual(m.main(['--pr', '506', '--report', str(path)]), 1)
            self.assertEqual(json.loads(path.read_text())['status'], 'rejected')
            self.assertFalse(json.loads(path.read_text())['deployed'])

    def test_invalid_pr_and_commit_fail_before_network(self):
        with patch.object(m.checks, 'api') as api:
            for args in [{'pr_number': 'main'}, {'commit': 'main'}, {}, {'pr_number': '1', 'commit': 'b' * 40}]:
                with self.subTest(args=args), self.assertRaises(ValueError):
                    m.prepare(**args)
            api.assert_not_called()

    def test_workflow_is_read_only_and_cannot_choose_an_executor(self):
        workflow = (ROOT / '.github/workflows/production-release-prepare.yml').read_text()
        for denied in ['id-token:', 'configure-aws-credentials', 'role-to-assume', 'kubectl ', 'helm ', '--apply', 'environment: production']:
            self.assertNotIn(denied, workflow)
        self.assertIn("if: github.ref == 'refs/heads/main'", workflow)
        self.assertIn('ref: ${{ github.sha }}', workflow)
        self.assertIn('paths: [\'deploy/v2/image-manifest.json\']', workflow)


if __name__ == '__main__':
    unittest.main()
