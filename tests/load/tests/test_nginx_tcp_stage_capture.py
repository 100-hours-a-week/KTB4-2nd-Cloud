import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location('tcp445', Path(__file__).parents[1]/'scripts/nginx-tcp-stage-capture.py')
cap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cap)


class TcpCaptureTest(unittest.TestCase):
    def test_only_flags_and_flow_metadata_survive(self):
        r = cap.parse_packet('1793000000.123 IP 8.8.8.8.50000 > 10.30.0.174.443: Flags [S], seq 1, win 1000, length 0', {'8.8.8.8'}, b'fixed-test-key')
        self.assertEqual(r['direction'], 'in')
        self.assertEqual(r['flags'], 'S')
        self.assertEqual(r['clientPort'], 50000)
        self.assertNotIn('8.8.8.8', str(r))
        reverse = cap.parse_packet('1793000000.124 IP 10.30.0.174.443 > 8.8.8.8.50000: Flags [S.], seq 2, ack 2, length 0', {'8.8.8.8'}, b'fixed-test-key')
        self.assertEqual(reverse['flowId'], r['flowId'])

    def test_ipv6_and_unknown_source(self):
        r = cap.parse_packet('1793000000.1 IP6 2001:4860:4860::8888.10000 > 2001:4860::1.443: Flags [.], ack 1, length 0', {'2001:4860:4860::8888'}, b'x')
        self.assertEqual(r['family'], 6)
        with self.assertRaises(ValueError): cap.parse_packet('1793000000.123 IP 8.8.8.8.1 > 10.30.0.174.443: Flags [S], length 0', {'1.1.1.1'}, b'x')

    def test_broad_target_and_unbounded_window_refused(self):
        a = SimpleNamespace(run_id='b9507e84-5b21-4b5f-b8d0-151b0124f56c', confirm_target=cap.ORIGIN, source_ip=['0.0.0.0'], seconds=30)
        with self.assertRaises(ValueError): cap.validate(a)
        a.source_ip=['ff02::1']
        with self.assertRaises(ValueError): cap.validate(a)
        a.source_ip=['8.8.8.8'];a.seconds=1801
        with self.assertRaises(ValueError): cap.validate(a)
        a.seconds=30;a.confirm_target='https://yeodam-2gether.com'
        with self.assertRaises(ValueError): cap.validate(a)


if __name__ == '__main__': unittest.main()
