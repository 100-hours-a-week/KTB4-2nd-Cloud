import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/cloud-pr-scope.py'
SPEC = importlib.util.spec_from_file_location('cloud_pr_scope', SCRIPT)
scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scope)
WORKFLOW = ROOT / '.github/workflows/cloud-pr-gate.yml'


class SelectionTest(unittest.TestCase):
    def assert_selection(self, paths, v1, v2):
        result = scope.select(paths)
        self.assertIs(result['runtime_required'], v1)
        self.assertIs(result['v2_runtime_required'], v2)

    def test_reviewed_v1_inputs(self):
        for path in scope.V1_FILES:
            with self.subTest(path=path):
                self.assert_selection([path], True, False)

    def test_v2_inputs_including_external_connector_dependency(self):
        for path in [*scope.V2_FILES, 'deploy/v2/image-manifest.json',
                     'tests/ci/v2/prepare.py', 'tests/ci/v2/new-input.md']:
            with self.subTest(path=path):
                self.assert_selection([path], False, True)

    def test_documentation_and_mixed_changes(self):
        self.assert_selection(['README.md', 'docs/design.pdf'], False, False)
        self.assert_selection(['README.md', 'deploy/image-versions.env'], True, False)
        self.assert_selection(['docs/design.md', 'deploy/v2/image-manifest.json'], False, True)
        self.assert_selection(['deploy/image-versions.env', 'deploy/v2/image-manifest.json'], True, True)

    def test_shared_unknown_and_empty_require_both(self):
        for path in ['.github/workflows/cloud-pr-gate.yml', 'scripts/cloud-pr-scope.py',
                     'scripts/validate-cloud-pr.sh', 'tests/ci/test_cloud_pr_scope.py',
                     '.github/new-config.yml', 'terraform/new.tf', 'scripts/new-dependency.py',
                     'deploy/v2-staging/kafka/compose.yml', 'unknown space\nand newline']:
            with self.subTest(path=path):
                self.assert_selection([path], True, True)
        self.assert_selection([], True, True)


class GitDiffTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'scope-test')
        self.git('config', 'user.email', 'scope-test@example.invalid')
        self.write('README.md', 'base')
        self.write('tests/ci/seed.sql', 'select 1;')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root, text=True)

    def write(self, path, value):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(value)

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-qm', 'change')
        return self.git('rev-parse', 'HEAD').strip()

    def test_rename_between_stacks_checks_both_old_and_new_paths(self):
        (self.root / 'deploy/v2').mkdir(parents=True, exist_ok=True)
        self.git('mv', 'tests/ci/seed.sql', 'deploy/v2/seed.sql')
        paths = scope.changed_paths(self.base, self.commit(), self.root)
        self.assertEqual(set(paths), {'tests/ci/seed.sql', 'deploy/v2/seed.sql'})
        self.assertTrue(scope.select(paths)['runtime_required'])
        self.assertTrue(scope.select(paths)['v2_runtime_required'])

    def test_deletion_and_unusual_filenames_are_not_lost(self):
        (self.root / 'tests/ci/seed.sql').unlink()
        unusual = 'unknown space\nand newline'
        self.write(unusual, 'new')
        paths = scope.changed_paths(self.base, self.commit(), self.root)
        self.assertEqual(set(paths), {'tests/ci/seed.sql', unusual})
        self.assertTrue(scope.select(paths)['v2_runtime_required'])

    def test_base_only_changes_are_excluded_from_pr_diff(self):
        self.write('README.md', 'head')
        head = self.commit()
        self.git('checkout', '-q', '--detach', self.base)
        self.write('shared-new.py', 'base advanced')
        advanced_base = self.commit()
        self.assertEqual(scope.changed_paths(advanced_base, head, self.root), ['README.md'])

    def test_cli_report_outputs_and_git_error_fail_closed(self):
        self.write('deploy/v2/image-manifest.json', '{}')
        head = self.commit()
        output = self.root / 'outputs'
        report = self.root / 'report.json'
        args = ['python3', str(SCRIPT), '--base', self.base, '--head', head,
                '--output', str(output), '--report', str(report)]
        subprocess.run(args, cwd=self.root, check=True, capture_output=True)
        self.assertEqual(output.read_text(), 'runtime_required=false\nv2_runtime_required=true\n')
        self.assertFalse(json.loads(report.read_text())['runtime_required'])
        output.unlink()
        report.unlink()
        args[args.index('--base') + 1] = '0' * 40
        result = subprocess.run(args, cwd=self.root, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())
        with self.assertRaises(ValueError):
            scope.changed_paths('--not-a-sha', head, self.root)


class RequiredGateTest(unittest.TestCase):
    """Execute the actual final Workflow shell, including failure transitions."""
    @classmethod
    def setUpClass(cls):
        source = WORKFLOW.read_text()
        final_step = source.split('      - name: Require validation and applicable integration\n', 1)[1]
        cls.shell = textwrap.dedent(final_step.split('        run: |\n', 1)[1])

    def gate(self, v1, v2, r1, r2, validate='success'):
        env = os.environ | {'RUNTIME_REQUIRED': v1, 'V2_RUNTIME_REQUIRED': v2,
                            'INTEGRATION_RESULT': r1, 'V2_INTEGRATION_RESULT': r2,
                            'VALIDATE_RESULT': validate}
        return subprocess.run(['bash', '-c', self.shell], env=env, capture_output=True).returncode

    def test_required_success_and_intentional_skips(self):
        for v1, v2 in [('true', 'true'), ('true', 'false'), ('false', 'true'), ('false', 'false')]:
            with self.subTest(v1=v1, v2=v2):
                self.assertEqual(self.gate(v1, v2, 'success' if v1 == 'true' else 'skipped',
                                          'success' if v2 == 'true' else 'skipped'), 0)

    def test_required_failure_cancellation_and_unexpected_skip_block_gate(self):
        for result in ['failure', 'cancelled', 'skipped', 'timed_out', '']:
            with self.subTest(result=result):
                self.assertNotEqual(self.gate('true', 'false', result, 'skipped'), 0)
                self.assertNotEqual(self.gate('false', 'true', 'skipped', result), 0)

    def test_invalid_flags_or_validation_failure_cannot_pass(self):
        for flag in ['', 'unknown', 'TRUE']:
            self.assertNotEqual(self.gate(flag, 'false', 'skipped', 'skipped'), 0)
            self.assertNotEqual(self.gate('false', flag, 'skipped', 'skipped'), 0)
        for result in ['failure', 'cancelled', 'skipped', '']:
            self.assertNotEqual(self.gate('false', 'false', 'skipped', 'skipped', result), 0)
        self.assertNotEqual(self.gate('false', 'false', 'success', 'skipped'), 0)


if __name__ == '__main__':
    unittest.main()
