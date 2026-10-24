import base64
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('window', Path(__file__).parents[1]/'scripts/check-access-token-window.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def token(claims):
    return 'header.'+base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')+'.secret-signature'


class TokenWindowTest(unittest.TestCase):
    def test_valid_timestamps_do_not_expose_other_claims(self):
        raw = token({'iat':1000,'exp':2800,'sub':'private-user','sid':'private-session'})
        result = module.inspect(raw,900,1100)
        self.assertTrue(result['windowSufficient'])
        self.assertFalse(result['signatureVerified'])
        for secret in [raw,'private-user','private-session','secret-signature']:
            self.assertNotIn(secret,json.dumps(result))

    def test_short_window_and_expired_token_are_rejected(self):
        raw=token({'iat':1000,'exp':2800})
        self.assertFalse(module.inspect(raw,900,2500)['windowSufficient'])
        self.assertFalse(module.inspect(raw,0,2800)['windowSufficient'])

    def test_invalid_claims_do_not_leak_payload(self):
        for claims in [{'iat':1000,'exp':'private-payload'},{'iat':1000,'exp':True},{'iat':3000,'exp':2000}]:
            with self.assertRaises(ValueError) as failure:module.inspect(token(claims),900,1100)
            self.assertNotIn('private-payload',str(failure.exception))
