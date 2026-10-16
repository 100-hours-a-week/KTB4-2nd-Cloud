import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('staging_alerts', ROOT/'monitoring/v2-staging/configure-alerts.py')
alerts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alerts)

class StagingAlertsTest(unittest.TestCase):
    def test_policy_preserves_other_routes_and_is_idempotent(self):
        original = {'receiver':'original', 'routes':[{'receiver':'other','object_matchers':[['team','=','other']]}]}
        first = alerts.merge_policy(original)
        self.assertEqual(first['routes'][1], original['routes'][0])
        self.assertEqual(first['receiver'], 'original')
        self.assertEqual(original['routes'][0]['receiver'], 'other')
        self.assertEqual(alerts.merge_policy(first), first)
        self.assertEqual(first['routes'][0]['object_matchers'], [alerts.SCOPE])

    def test_paused_install_and_scoped_rule(self):
        s = json.loads((ROOT/'monitoring/v2-staging/alerts.json.tftpl').read_text())[0]
        rule = alerts.build_rule(s)
        self.assertTrue(rule['isPaused'])
        self.assertFalse(alerts.build_rule(s, paused=False)['isPaused'])
        self.assertEqual(rule['labels']['monitoring_scope'], 'v2-staging')
        self.assertEqual(rule['execErrState'], 'Error')

    def test_verification_requires_separate_confirmation(self):
        with self.assertRaisesRegex(ValueError, 'confirm-two-messages'):
            alerts.verify(None, False)

    def test_secret_error_body_not_logged(self):
        import urllib.error
        import io
        client = object.__new__(alerts.Grafana)
        client.headers = {}
        error = urllib.error.HTTPError('http://localhost',400,'invalid',{},io.BytesIO(b'secret-webhook'))
        with patch.object(alerts.urllib.request,'urlopen',side_effect=error):
            with self.assertRaisesRegex(RuntimeError,'HTTP 400') as caught:
                client.api('POST',alerts.BASE+'/contact-points',{})
        self.assertNotIn('secret-webhook',str(caught.exception))

if __name__ == '__main__': unittest.main()
