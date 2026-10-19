import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/install-v2-production-kafka.sh'


class HostBoundaryTest(unittest.TestCase):
    def run_script(self, expected='i-aaa', actual='i-aaa'):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            commands = {
                'id': '#!/bin/sh\necho 0\n',
                'curl': '#!/bin/sh\ncase "$*" in *api/token*) echo fixture-token;; *) echo "$FAKE_INSTANCE";; esac\n',
                'mountpoint': '#!/bin/sh\nexit 1\n',
            }
            for name, text in commands.items():
                path = base / name
                path.write_text(text)
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(base) + os.pathsep + os.environ['PATH'],
                       EXPECTED_INSTANCE_ID=expected, FAKE_INSTANCE=actual)
            return subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True)

    def test_missing_instance_rejected_before_mount_or_install(self):
        result = self.run_script(expected='')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Missing expected', result.stderr)

    def test_malformed_instance_rejected(self):
        result = self.run_script(expected='i-aaa; echo unsafe')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Missing expected', result.stderr)

    def test_staging_or_other_host_cannot_be_installed(self):
        result = self.run_script(actual='i-bbb')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Wrong production Kafka host', result.stderr)

    def test_correct_host_without_data_mount_still_cannot_install(self):
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Kafka data EBS is not mounted', result.stderr)


if __name__ == '__main__':
    unittest.main()
