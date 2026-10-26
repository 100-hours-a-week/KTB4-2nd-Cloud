import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'monitoring/v1-staging'))
from nginx_observability import instrument_site
from nginx_read_enforcement import instrument_read_enforcement
from nginx_create_dry_run import instrument_create_dry_run, LOCATION
from nginx_create_enforcement import instrument_create_enforcement, instrument_create_observation, ENFORCED_LOCATION, LEGACY_ENFORCED_LOCATION


class EnforcementTest(unittest.TestCase):
    def test_only_create_mode_changes_and_timing_upgrade_keeps_routes(self):
        source = instrument_create_dry_run(instrument_read_enforcement(instrument_site(
            (ROOT/'nginx/yeodam.conf').read_text().replace('yeodam-2gether.com', 'v1-staging.yeodam-2gether.com'))))
        applied = instrument_create_enforcement(source)
        self.assertEqual(applied.replace(ENFORCED_LOCATION, LOCATION, 1), source)
        self.assertEqual(instrument_create_enforcement(applied), applied)
        self.assertEqual(instrument_create_observation(applied), source)
        self.assertEqual(instrument_create_enforcement(source.replace(LOCATION, LEGACY_ENFORCED_LOCATION)), applied)
        older = source.replace('\n      \'"upstream_header_time":"$upstream_header_time",\'', '')
        upgraded = instrument_site(older)
        self.assertEqual(upgraded.replace('\n      \'"upstream_header_time":"$upstream_header_time",\'', ''), older)
        self.assertEqual(instrument_site(upgraded), upgraded)
        with self.assertRaises(ValueError):
            instrument_create_enforcement(source.replace('burst=5 nodelay;', 'burst=50 nodelay;'))


if __name__ == '__main__':
    unittest.main()
