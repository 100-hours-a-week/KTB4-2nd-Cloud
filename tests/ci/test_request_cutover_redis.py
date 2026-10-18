"""Actual Redis types, absolute expiry and pre-copy deletion/rotation reconciliation."""
import os
from pathlib import Path
import sys
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from request_cutover.auth import Entry, replace_inactive, verify
import redis


class ProjectionIntegration(unittest.TestCase):
    def test_expiry_types_deletions_and_rotated_reverse_index(self):
        client = redis.Redis(host=os.environ['CUTOVER_TEST_REDIS_HOST'], decode_responses=True)
        prefix = 'isolated-test:' + str(uuid4()) + ':'
        now = client.time()[0] * 1000
        end = now + 60000
        try:
            initial = {prefix + 'session:sid': Entry('hash', {'refreshTokenHash': 'old'}, end),
                       prefix + 'refresh:old': Entry('string', 'sid', end),
                       prefix + 'csrf:deleted': Entry('string', 'csrf', end),
                       prefix + 'login-ticket:consumed': Entry('string', '{}', end)}
            replace_inactive(client, initial, prefix)
            verify(client, initial, prefix, now)
            final = {prefix + 'session:sid': Entry('hash', {'refreshTokenHash': 'new'}, end),
                     prefix + 'refresh:new': Entry('string', 'sid', end),
                     prefix + 'user-sessions:1': Entry('zset', {'sid': end}, end),
                     prefix + 'expired': Entry('string', 'never-resurrect', now - 1)}
            replace_inactive(client, final, prefix)
            self.assertEqual(verify(client, final, prefix, now)['keys'], 3)
            for suffix in ('refresh:old', 'csrf:deleted', 'login-ticket:consumed', 'expired'):
                self.assertIsNone(client.get(prefix + suffix))
            self.assertEqual(client.pexpiretime(prefix + 'session:sid'), end)
            client.hset(prefix + 'session:sid', 'refreshTokenHash', 'wrong')
            with self.assertRaises(ValueError):
                verify(client, final, prefix, now)
        finally:
            keys = list(client.scan_iter(match=prefix + '*'))
            if keys:
                client.delete(*keys)


if __name__ == '__main__':
    unittest.main()
