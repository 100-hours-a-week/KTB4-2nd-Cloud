import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('nginx_obs', ROOT/'monitoring/v1-staging/nginx_observability.py')
obs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(obs)


class InstrumentationTest(unittest.TestCase):
    def test_only_format_changes_and_second_apply_is_noop(self):
        original = (ROOT/'nginx/yeodam.conf').read_text()
        for escaped in [False, True]:
            with self.subTest(escaped=escaped):
                source = original.replace('"', r'\"') if escaped else original
                result = obs.instrument_site(source)
                quote = r'\"' if escaped else '"'
                for key, value in obs.FIELDS.items():
                    self.assertEqual(result.count(quote+key+quote+':'+quote+value+quote), 1)
                remaining = '\n'.join(line for line in result.splitlines() if not any(quote+k+quote in line for k in obs.FIELDS))+'\n'
                self.assertEqual(remaining, source)
                self.assertEqual(obs.instrument_site(result), result)

    def test_partial_and_unknown_format_refuse_rewrite(self):
        for source in ['server { listen 80; }', 'log_format yeodam_json escape=json \'"connection":"$connection",\';']:
            with self.assertRaises(ValueError): obs.instrument_site(source)

    def test_upgrade_existing_connection_fields_without_replacing_routing(self):
        source = (ROOT/'nginx/yeodam.conf').read_text()
        complete = obs.instrument_site(source)
        old = '\n'.join(line for line in complete.splitlines()
                        if not any('"'+k+'"' in line for k in obs.MEASUREMENT_FIELDS))+'\n'
        self.assertEqual(obs.instrument_site(old).count('"peer_addr":"$remote_addr"'), 1)
        self.assertEqual(obs.instrument_site(complete), complete)
        with self.assertRaises(ValueError):
            obs.instrument_site(complete.replace('$remote_addr', '$http_x_forwarded_for'))

    def test_scrape_preserves_existing_file_readers(self):
        source = 'loki.source.file "existing" {}\nprometheus.remote_write "staging" {}\n'
        result = obs.instrument_alloy(source)
        self.assertTrue(result.startswith(source))
        self.assertIn('127.0.0.1:9113', result)
        self.assertEqual(obs.instrument_alloy(result), result)
        with self.assertRaises(ValueError):obs.instrument_alloy('prometheus.scrape "v1_nginx" {}')
