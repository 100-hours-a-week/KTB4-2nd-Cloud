import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'terraform/v2/production/files/provision-mysql-app-account.sh'


class AccountBoundaryTest(unittest.TestCase):
    def run_script(self, instance='i-aaa', tables=0, channels=0, password=None):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake = '''#!/usr/bin/env python3
import os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
if name == 'curl':
    print('fixture-token' if 'PUT' in sys.argv else os.environ['FAKE_INSTANCE'])
elif name == 'aws':
    print(os.environ['FAKE_PASSWORD'])
elif 'information_schema.tables' in ' '.join(sys.argv):
    print(os.environ['FAKE_TABLES'])
    print(os.environ['FAKE_CHANNELS'])
else:
    Path(os.environ['DDL_PATH']).write_text(sys.stdin.read())
'''
            for name in ['aws', 'curl', 'docker']:
                path = base / name
                path.write_text(fake)
                path.chmod(0o755)
            ddl = base / 'ddl.sql'
            env = dict(os.environ, PATH=str(base) + os.pathsep + os.environ['PATH'],
                       EXPECTED_INSTANCE_ID='i-aaa', FAKE_INSTANCE=instance,
                       FAKE_TABLES=str(tables), FAKE_CHANNELS=str(channels),
                       FAKE_PASSWORD=password or 'a' * 64, DDL_PATH=str(ddl))
            result = subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True)
            return result, ddl.read_text() if ddl.exists() else None

    def test_wrong_host_cannot_provision(self):
        result, ddl = self.run_script(instance='i-bbb')
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(ddl)

    def test_nonempty_database_cannot_change_credentials(self):
        result, ddl = self.run_script(tables=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(ddl)

    def test_replica_cannot_change_credentials(self):
        result, ddl = self.run_script(channels=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(ddl)

    def test_malformed_secret_is_not_interpolated(self):
        result, ddl = self.run_script(password="';DROP DATABASE yeodam;--")
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(ddl)

    def test_empty_new_host_gets_only_schema_scoped_grant(self):
        result, ddl = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ON yeodam.* TO 'yeodam_app'@'10.20.10.%'", ddl)
        self.assertNotIn('ON *.*', ddl)
        self.assertNotIn('WITH GRANT OPTION', ddl)
        self.assertNotIn('a' * 64, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
