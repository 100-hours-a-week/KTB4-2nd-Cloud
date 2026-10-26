from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'monitoring/v1-staging'))
from nginx_observability import instrument_site
from nginx_read_dry_run import PREFIX, SERVER, instrument_read_dry_run
from nginx_read_enforcement import ENFORCED_PREFIX, ENFORCED_SERVER, instrument_read_enforcement


class EnforcementTest(unittest.TestCase):
    def source(self):
        return instrument_site((ROOT/'nginx/yeodam.conf').read_text().replace(
            'yeodam-2gether.com', 'v1-staging.yeodam-2gether.com'))

    def test_promotion_changes_only_mode_and_comments_and_is_idempotent(self):
        dry = instrument_read_dry_run(self.source())
        actual = instrument_read_enforcement(dry)
        self.assertEqual(actual.replace(ENFORCED_PREFIX, PREFIX).replace(ENFORCED_SERVER, SERVER), dry)
        self.assertEqual(instrument_read_enforcement(actual), actual)
        self.assertEqual(instrument_read_enforcement(self.source()), actual)

    def test_partial_foreign_or_modified_policy_is_rejected(self):
        actual = instrument_read_enforcement(self.source())
        for bad in [actual.replace('burst=30', 'burst=31'), actual.replace('limit_req_dry_run off;', ''),
                    actual.replace(ENFORCED_PREFIX, PREFIX),
                    (ROOT/'nginx/yeodam.conf').read_text(), actual+'\nlimit_req zone=other;\n']:
            with self.subTest(), self.assertRaises(ValueError):
                instrument_read_enforcement(bad)


if __name__ == '__main__':
    unittest.main()
