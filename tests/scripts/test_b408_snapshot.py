import importlib.util
import io
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('snapshot408', ROOT/'scripts/b-integration408-snapshot.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class SnapshotPacing(unittest.TestCase):
    def test_pacing_preserves_all_bytes_and_bounds_progress(self):
        now = [0.0]
        def sleep(seconds): now[0] += seconds
        data = bytes(range(256)) * 1025
        output = io.BytesIO()
        result = m.transfer(io.BytesIO(data), output, 65536,
                            clock=lambda: now[0], sleep=sleep)
        self.assertEqual(output.getvalue(), data)
        self.assertAlmostEqual(now[0], len(data)/65536)
        self.assertEqual(result['uncompressedBytes'], len(data))

    def test_control_does_not_sleep(self):
        output = io.BytesIO()
        def unexpected(_): raise AssertionError('Control must not be paced')
        m.transfer(io.BytesIO(b'unchanged'), output, 0, sleep=unexpected)
        self.assertEqual(output.getvalue(), b'unchanged')

    def test_render_changes_only_stream_stage(self):
        template = (ROOT/'terraform/v2/staging/files/capture-mysql-replication-source.sh').read_text()
        run = '00000000-0000-4000-8000-000000000408'
        result = m.render(template, run, 1048576)
        self.assertIn('--single-transaction --quick --source-data=2', result)
        self.assertEqual(result.replace('  | python3 /run/yeodam-b391/b-integration408-snapshot.py stream --run-id '+run+' --rate 1048576 \\\n', ''), template)
        with self.assertRaises(ValueError): m.render(template.replace('gzip -1', 'gzip -9'), run, 0)


if __name__ == '__main__': unittest.main()
