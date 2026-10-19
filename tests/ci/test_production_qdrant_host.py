import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/install-v2-production-qdrant.sh'


class QdrantHostBoundaryTest(unittest.TestCase):
    def run_installer(self, expected='i-aaa', actual='i-aaa', mounted='1', serial='volbbb'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = {
                'id': '#!/bin/sh\necho 0\n',
                'curl': '#!/bin/sh\ncase "$*" in *api/token*) echo fixture-token;; *) echo "$FAKE_INSTANCE";; esac\n',
                'mountpoint': '#!/bin/sh\n[ "$FAKE_MOUNTED" = 1 ]\n',
                'findmnt': '#!/bin/sh\necho /dev/fixture-volume\n',
                'lsblk': '#!/bin/sh\necho "$FAKE_SERIAL"\n',
            }
            for name, text in commands.items():
                path = root / name
                path.write_text(text)
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                       EXPECTED_INSTANCE_ID=expected, EXPECTED_DATA_VOLUME='vol-aaa',
                       FAKE_INSTANCE=actual, FAKE_MOUNTED=mounted, FAKE_SERIAL=serial)
            return subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True)

    def test_missing_host_rejected(self):
        result = self.run_installer(expected='')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Missing expected GPU instance ID', result.stderr)

    def test_other_host_rejected(self):
        result = self.run_installer(actual='i-bbb')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Wrong shared GPU host', result.stderr)

    def test_unmounted_data_rejected(self):
        result = self.run_installer(mounted='0')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Qdrant data EBS is not mounted', result.stderr)

    def test_other_volume_rejected(self):
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Wrong Qdrant data EBS', result.stderr)


if __name__ == '__main__':
    unittest.main()
