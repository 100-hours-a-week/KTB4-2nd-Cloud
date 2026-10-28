import importlib.util
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'monitoring/v1-staging'))


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


prepare = module('prepare_user_read', ROOT/'monitoring/v1-staging/prepare-user-read-cutover.py')
renderer = module('staging_renderer', ROOT/'scripts/render-v1-staging-nginx.py')


class UserReadCutoverTest(unittest.TestCase):
    def source(self):
        return renderer.render((ROOT/'nginx/yeodam.conf').read_text(), 'a'*40)

    def test_candidate_changes_only_explicit_read_rate_and_burst(self):
        site = self.source()
        actual = prepare.candidate(site, 17, 43)  # Mechanical fixture, not a proposed production limit.
        self.assertEqual(actual.replace('rate=17r/s;', 'rate=5r/s;').replace(
            'burst=43 nodelay;', 'burst=30 nodelay;'), site)
        self.assertIn('rate=1r/s;', actual)
        self.assertIn('burst=5 nodelay;', actual)
        self.assertIn('keepalive_timeout 15s;', actual)

    def test_foreign_partial_or_previously_modified_policies_are_rejected(self):
        source = self.source()
        for site in [source.replace('v1-staging.', ''), source.replace('rate=5r/s;', 'rate=6r/s;'),
                     source.replace('burst=30 nodelay;', 'burst=31 nodelay;'),
                     source.replace(prepare.ENFORCED_LOCATION, ''), source+prepare.ENFORCED_PREFIX]:
            with self.subTest(), self.assertRaises(ValueError):
                prepare.candidate(site, 17, 43)

    def test_no_implicit_or_stricter_ip_policy(self):
        for rate, burst in [(5, 30), (0, 30), (17, 29), (100001, 30), (17, 100001)]:
            with self.subTest(), self.assertRaises(ValueError):
                prepare.candidate(self.source(), rate, burst)

    def test_cli_preserves_input_and_rejects_wrong_checksum_or_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'site'
            output = Path(directory)/'candidate'
            original = self.source().encode()
            source.write_bytes(original)
            command = [sys.executable, str(ROOT/'monitoring/v1-staging/prepare-user-read-cutover.py'),
                       '--site', str(source), '--ip-rate', '17', '--ip-burst', '43', '--output', str(output),
                       '--expected-site-sha256']
            bad = subprocess.run(command+['0'*64], capture_output=True)
            self.assertNotEqual(bad.returncode, 0)
            self.assertFalse(output.exists())
            checksum = hashlib.sha256(original).hexdigest()
            subprocess.run(command+[checksum], check=True, capture_output=True)
            self.assertEqual(source.read_bytes(), original)
            rendered = output.read_bytes()
            self.assertNotEqual(subprocess.run(command+[checksum], capture_output=True).returncode, 0)
            self.assertEqual(output.read_bytes(), rendered)


if __name__ == '__main__':
    unittest.main()
