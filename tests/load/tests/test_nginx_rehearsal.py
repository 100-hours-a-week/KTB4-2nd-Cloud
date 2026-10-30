import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1]/'scripts'/filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


capture = module('capture426', 'nginx-rehearsal.py')
archive = module('archive426', 'nginx-rehearsal-archive.py')
fetcher = module('fetch426', 'fetch-nginx-rehearsal.py')


class RehearsalTest(unittest.TestCase):
    def test_stub_failure_keeps_host_cpu_and_process_sample(self):
        class Detail:
            def sample(self):
                return {'processes': [{'pid': 1, 'rssKiB': 123}]}
        with patch.object(capture, 'resources', return_value={'memoryAvailableKiB': 5000}), \
             patch.object(capture.observer, 'cpu', return_value=(200, 150)), \
             patch.object(capture.subprocess, 'check_output', return_value='0 0 local:443 peer:1234\n'), \
             patch.object(capture.observer, 'request', side_effect=ConnectionResetError):
            row, before = capture.independent_sample(Detail(), (100, 75))
        self.assertEqual(row['memoryAvailableKiB'], 5000)
        self.assertEqual(row['cpuPercent'], 25)
        self.assertEqual(row['httpsEstablished'], 1)
        self.assertEqual(row['processes'][0]['rssKiB'], 123)
        self.assertEqual(row['componentErrors'], {'stub': 'ConnectionResetError'})
        self.assertNotIn('active', row)
        self.assertEqual(before, (200, 150))

    def test_rotation_drains_old_inode_then_new_file_without_copytruncate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root/'source', root/'out'
            source.write_bytes(b'before capture\n')
            follower = capture.LogFollower(source, output)
            with source.open('ab') as f:
                f.write(b'A\n')
            follower.drain(100)
            source.rename(root/'source.1')
            with (root/'source.1').open('ab') as f:
                f.write(b'B\n')
            source.write_bytes(b'C\n')
            follower.drain(100)
            follower.drain(100)
            self.assertEqual(output.read_bytes(), b'A\nB\nC\n')
            self.assertEqual(follower.state()['rotations'], 1)
            self.assertEqual(follower.state()['unreadBytes'], 0)
            follower.close()

    def test_truncation_and_unread_backlog_are_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source'
            source.write_bytes(b'initial data\n')
            follower = capture.LogFollower(source, root/'out')
            source.write_bytes(b'fresh\n')
            follower.drain(1)
            self.assertTrue(follower.state()['copyTruncateDetected'])
            self.assertEqual(follower.state()['unreadBytes'], 5)
            follower.close()

    def test_loki_splits_full_pages_and_preserves_boundary_entries(self):
        events = [(x, str(x)) for x in range(0, 30, 2)]
        def query(_, left, right, limit):
            rows = [(str(at), body) for at, body in events if left <= at < right][:limit]
            return {'data': {'result': [{'values': rows}]}}
        output = []
        archive.loki_pages(query, 'query', 0, 30, lambda t, line: output.append((t, line)), limit=4)
        self.assertEqual(output, events)

    def test_loki_saturated_single_timestamp_is_not_silently_skipped(self):
        def query(_, left, right, limit):
            return {'data': {'result': [{'values': [('10', 'same')]*limit if left <= 10 < right else []}]}}
        with self.assertRaisesRegex(ValueError, 'Single timestamp'):
            archive.loki_pages(query, 'query', 0, 20, lambda *_: None, limit=4)

    def test_export_excludes_credentials_query_and_peer(self):
        data = {'peer_addr': '203.0.113.9', 'status': 200, 'request_id': 'rid',
                'cookie': 'private', 'request_uri': '/?accessToken=private', 'authorization': 'Bearer opaque'}
        row = archive.access_row(data, b'key')
        self.assertEqual(row['request_id'], 'rid')
        text = json.dumps(row)
        for forbidden in ['203.0.113.9', 'private', 'opaque']:
            self.assertNotIn(forbidden, text)
        self.assertNotIn('opaque', archive.scrub('Authorization: Bearer opaque', b'key'))

    def test_export_retains_only_finite_jwt_classification_not_token_identity(self):
        row = archive.access_row({'peer_addr':'203.0.113.9','jwt_gate_result':'signature_valid',
                                  'accessToken':'private','userId':'123'},b'key')
        self.assertEqual(row['jwt_gate_result'],'signature_valid')
        self.assertNotIn('private',json.dumps(row))
        self.assertNotIn('userId',row)
        with self.assertRaises(ValueError):
            archive.access_row({'jwt_gate_result':'arbitrary-private-value'},b'key')

    def test_cloudwatch_empty_page_is_not_end_and_ingestion_time_is_kept(self):
        pages = [{'events': [], 'nextToken': 'next'}, {'events': [
            {'eventId': 'a', 'timestamp': 10, 'ingestionTime': 20, 'message': '{"event":"done"}'}]}]
        output = io.StringIO()
        count = fetcher.cloudwatch(lambda _: pages.pop(0), 'group', 0, 30, output, b'key')
        self.assertEqual(count, 1)
        self.assertEqual(json.loads(output.getvalue())['ingestionTime'], 20)

    def test_tar_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'bad.tar.gz'
            with tarfile.open(path, 'w:gz') as tar:
                info = tarfile.TarInfo('../escape')
                info.size = 1
                tar.addfile(info, io.BytesIO(b'x'))
            target = root/'out'
            target.mkdir()
            with self.assertRaises(ValueError):
                fetcher.extract(path, target)
            self.assertFalse((root/'escape').exists())


if __name__ == '__main__':
    unittest.main()
