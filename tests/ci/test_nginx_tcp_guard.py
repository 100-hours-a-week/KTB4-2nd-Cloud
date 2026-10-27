import importlib.util
from pathlib import Path
import unittest

p=Path(__file__).parents[2]/'monitoring/v1-staging/nginx_tcp_guard.py'
spec=importlib.util.spec_from_file_location('tcp436',p);guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)


class GuardTest(unittest.TestCase):
    def test_capacity_preserves_routes_and_worker_count(self):
        source='worker_processes auto;\nevents {\n worker_connections 768;\n}\nhttp { include sites-enabled/*; }\n'
        updated=guard.capacity(source,1536)
        self.assertEqual(guard.capacity(updated,1536),updated)
        self.assertIn('worker_processes auto;',updated)
        self.assertIn('http { include sites-enabled/*; }',updated)
        self.assertIn('worker_rlimit_nofile 4096;',updated)
        with self.assertRaises(ValueError):guard.capacity(source,100000)

    def test_counter_mode_does_not_drop_or_flush_other_firewalls(self):
        text=guard.nft_policy(32,20,40,False)
        self.assertNotIn('drop',text)
        self.assertNotIn('flush',text)
        self.assertNotIn('hook forward',text)
        self.assertIn('meta nfproto ipv4',text)
        self.assertIn('meta nfproto ipv6',text)
        self.assertIn('tcp dport { 80, 443 }',text)
        self.assertNotIn('22',text)
        self.assertIn('ct state new',text)

    def test_no_timeout_on_conncount_sets_and_bounded_dynamic_sets(self):
        text=guard.nft_policy(32,20,40,True)
        self.assertIn('set concurrent4 { type ipv4_addr; flags dynamic; size 16384; }',text)
        self.assertEqual(text.count(' drop'),4)
        self.assertEqual(guard.nft_policy(0,20,40,True).count(' drop'),2)
        self.assertEqual(guard.nft_policy(32,0,40,True).count(' drop'),2)


if __name__=='__main__':unittest.main()
