import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('ci_baseline', ROOT / 'scripts/ci-baseline.py')
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)

WORKFLOW = '''jobs:
  tests:
    name: Tests
    runs-on: ubuntu-latest
  image:
    name: Image
    runs-on: ubuntu-latest
  gate:
    name: Gate
    needs: [tests, image]
    runs-on: ubuntu-latest
'''


def stamp(seconds):
    return f'2026-10-16T00:{seconds // 60:02d}:{seconds % 60:02d}Z'


def job(name, start, end, conclusion='success', steps=None):
    return {'name': name, 'started_at': stamp(start), 'completed_at': stamp(end),
            'status': 'completed', 'conclusion': conclusion, 'steps': steps or []}


def payload(jobs, conclusion='success', attempt=1):
    return {'run': {'id': 1, 'run_attempt': attempt, 'created_at': stamp(0),
                    'run_started_at': stamp(100) if attempt > 1 else stamp(0),
                    'status': 'completed', 'conclusion': conclusion, 'head_sha': 'a' * 40},
            'jobs': jobs}


class CIBaselineTest(unittest.TestCase):
    def test_parallel_elapsed_is_not_runner_sum_and_gate_wait_excludes_needs(self):
        result, jobs, _ = ci.analyze_attempt('BE', payload([
            job('Tests', 5, 65), job('Image', 2, 32), job('Gate', 68, 70),
        ]), WORKFLOW, {})
        self.assertEqual(result['elapsed_seconds'], 70)
        self.assertEqual(result['runner_seconds'], 92)
        self.assertEqual(jobs[2]['ready_to_start_seconds'], 3)
        self.assertEqual(result['observed_completion_chain'], ['Tests', 'Gate'])

    def test_skipped_job_with_reversed_timestamps_is_not_negative_runner_time(self):
        result, jobs, _ = ci.analyze_attempt('Cloud', payload([
            job('Tests', 2, 12), job('Image', 1, 0, 'skipped'), job('Gate', 14, 16),
        ]), WORKFLOW, {})
        self.assertEqual(result['elapsed_seconds'], 16)
        self.assertEqual(result['runner_seconds'], 12)
        self.assertIsNone(jobs[1]['seconds'])
        self.assertEqual(result['skipped_jobs'], ['Image'])

    def test_rerun_origin_does_not_include_old_pr_wait(self):
        result, _, _ = ci.analyze_attempt('AI', payload([
            job('Tests', 102, 112), job('Image', 102, 120), job('Gate', 122, 124),
        ], attempt=2), WORKFLOW, {})
        self.assertEqual(result['elapsed_seconds'], 24)
        self.assertEqual(result['origin'], 'attempt-start')

    def test_failure_feedback_and_skipped_steps_are_preserved(self):
        steps = [
            {'name': 'Set up job', 'completed_at': stamp(4), 'conclusion': 'success'},
            {'name': 'Lint', 'started_at': stamp(5), 'completed_at': stamp(8), 'conclusion': 'failure'},
            {'name': 'Test', 'conclusion': 'skipped'},
        ]
        result, _, step_rows = ci.analyze_attempt('FE', payload([
            job('Tests', 2, 9, 'failure', steps), job('Image', 2, 30), job('Gate', 32, 34, 'failure'),
        ], 'failure'), WORKFLOW, {})
        self.assertEqual(result['first_failure_seconds'], 8)
        self.assertEqual(result['first_verification_step_seconds'], 8)
        self.assertIsNone(step_rows[-1]['seconds'])
        self.assertEqual(result['conclusion'], 'failure')

    def test_cancelled_unfinished_interval_is_not_success_sample(self):
        interrupted = job('Tests', 2, 3, 'cancelled')
        interrupted['completed_at'] = None
        result, _, _ = ci.analyze_attempt('BE', payload([interrupted], 'cancelled'), WORKFLOW, {})
        self.assertFalse(result['timing_complete'])
        self.assertIsNone(result['elapsed_seconds'])
        self.assertEqual(result['conclusion'], 'cancelled')

    def test_cache_and_pr_changes_are_unknown_even_for_document_commit(self):
        result, _, _ = ci.analyze_attempt('AI', payload([job('Tests', 1, 10)]), WORKFLOW,
                                         {'files_complete': True, 'files': ['README.md']})
        self.assertEqual(result['head_commit_change_kind'], 'documentation-only')
        self.assertEqual(result['pr_change_kind'], 'unknown')
        self.assertEqual(result['cache_state'], 'unknown')
        self.assertIsNone(result['checkout_sha'])
        self.assertFalse(result['dependency_graph_known'])

    def test_unknown_workflow_graph_is_not_treated_as_zero_queue(self):
        for source in [WORKFLOW.replace('needs: [tests, image]', 'needs: ${{ inputs.jobs }}'),
                       WORKFLOW.replace('name: Tests', 'name: ${{ matrix.name }}'),
                       WORKFLOW.replace('runs-on: ubuntu-latest', 'strategy: {}', 1),
                       WORKFLOW.replace('needs: [tests, image]', 'needs: [gate]')]:
            self.assertIsNone(ci.workflow_graph(source))
        result, jobs, _ = ci.analyze_attempt('BE', payload([job('Unexpected', 3, 6)]), WORKFLOW, {})
        self.assertFalse(result['dependency_graph_known'])
        self.assertIsNone(jobs[0]['ready_to_start_seconds'])

    def test_block_needs_and_no_name_are_supported(self):
        source = '''jobs:
  tests:
    runs-on: ubuntu-latest
  gate:
    needs:
      - tests
    runs-on: ubuntu-latest
'''
        self.assertEqual(ci.workflow_graph(source), {'tests': [], 'gate': ['tests']})

    def test_paginated_jobs_are_all_retained(self):
        with patch.object(ci, 'api', side_effect=[{'jobs': list(range(100))}, {'jobs': [100]}]) as request:
            self.assertEqual(len(ci.pages('repos/example/actions/jobs', 'jobs')), 101)
        self.assertIn('page=2', request.call_args.args[0])

    def test_read_only_api_never_prints_error_body_or_uses_shell(self):
        with patch.object(ci.subprocess, 'run') as execute:
            execute.return_value.returncode = 1
            execute.return_value.stderr = 'sensitive error reflection'
            with self.assertRaisesRegex(RuntimeError, r'GitHub GET failed \(1\)') as error:
                ci.api('repos/example/actions/runs')
        self.assertNotIn('sensitive', str(error.exception))
        self.assertEqual(execute.call_args.args[0][4:6], ['--method', 'GET'])
        self.assertNotIn('shell', execute.call_args.kwargs)

    def test_incomplete_or_empty_commit_is_unknown(self):
        self.assertEqual(ci.change_kind({'files': ['README.md'], 'files_complete': False}), 'unknown')
        self.assertEqual(ci.change_kind({'files': [], 'files_complete': True}), 'unknown')
        self.assertEqual(ci.change_kind({'files': ['uv.lock'], 'files_complete': True}), 'dependencies')

    def test_model_runtime_skip_changes_the_executed_verification_profile(self):
        step = {'name': 'Check model load and one-image inference', 'started_at': stamp(2),
                'completed_at': stamp(5), 'conclusion': 'success'}
        result, _, _ = ci.analyze_attempt('AI', payload([job('Tests', 1, 10, steps=[step])]), WORKFLOW, {})
        self.assertEqual(result['executed_verification_steps'], ['Tests: Check model load and one-image inference'])
        step['conclusion'] = 'skipped'
        result, _, _ = ci.analyze_attempt('AI', payload([job('Tests', 1, 10, steps=[step])]), WORKFLOW, {})
        self.assertEqual(result['executed_verification_steps'], [])

    def test_offline_report_keeps_failure_and_rerun_but_excludes_them_from_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sha = 'a' * 40
            ci.save(root / 'AI/commits' / f'{sha}.json', {})
            (root / 'AI/workflows').mkdir()
            (root / 'AI/workflows' / f'{sha}.yml').write_text(WORKFLOW)
            records = [('success', 1), ('failure', 1), ('success', 2)]
            paths = []
            for index, (result, attempt) in enumerate(records):
                item = payload([job('Tests', 102, 112), job('Image', 102, 120), job('Gate', 122, 124)], result, attempt)
                item['run']['id'] = index + 1
                relative = f'AI/attempts/{index}.json'
                paths.append(relative)
                ci.save(root / relative, item)
            ci.save(root / 'manifest.json', {'repositories': [{'repository': 'AI', 'runs': paths, 'errors': []}]})
            self.assertEqual(ci.analyze(root), 3)
            groups = json.loads((root / 'groups.json').read_text())
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]['count'], 1)
            self.assertIsNone(groups[0]['p90_seconds'])
            self.assertEqual(len(json.loads((root / 'summary.json').read_text())), 3)


if __name__ == '__main__':
    unittest.main()
