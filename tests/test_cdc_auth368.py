import importlib.util
import base64
import json
from datetime import datetime, timedelta
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("cdc_auth368", ROOT / "scripts" / "cdc_auth368.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

PREFIX = "yeodam:cutover:test368:"
BASE = datetime(2026, 10, 20, 13, 0, 0)
NOW = int(BASE.replace(tzinfo=ZoneInfo("Asia/Seoul")).timestamp() * 1000)


class MemoryRedis:
    def __init__(self):
        self.data = {}
        self.deadlines = {}

    def pipeline(self, transaction=True):
        assert transaction
        return Pipeline(self)

    def eval(self, script, key_count, key, now_ms):
        assert script == module.INDEX_EXPIRY_LUA and key_count == 1
        members = self.data.get(key, {})
        members = {sid: end for sid, end in members.items() if end > now_ms}
        if members:
            self.data[key] = members
            self.deadlines[key] = max(members.values())
            return 2
        self.data.pop(key, None)
        self.deadlines.pop(key, None)
        return 0


class Pipeline:
    def __init__(self, redis):
        self.redis = redis
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def delete(self, *keys):
        self.commands.append(("delete", keys))

    def zrem(self, key, sid):
        self.commands.append(("zrem", key, sid))

    def hset(self, key, mapping):
        self.commands.append(("hset", key, mapping))

    def pexpireat(self, key, end):
        self.commands.append(("pexpireat", key, end))

    def set(self, key, value, pxat):
        self.commands.append(("set", key, value, pxat))

    def zadd(self, key, members):
        self.commands.append(("zadd", key, members))

    def execute(self):
        for command in self.commands:
            name, *args = command
            if name == "delete":
                for key in args[0]:
                    self.redis.data.pop(key, None)
                    self.redis.deadlines.pop(key, None)
            elif name == "zrem":
                key, sid = args
                if key in self.redis.data:
                    self.redis.data[key].pop(sid, None)
            elif name == "hset":
                key, mapping = args
                self.redis.data[key] = dict(mapping)
            elif name == "pexpireat":
                key, end = args
                self.redis.deadlines[key] = end
            elif name == "set":
                key, value, end = args
                self.redis.data[key] = value
                self.redis.deadlines[key] = end
            elif name == "zadd":
                key, members = args
                self.redis.data.setdefault(key, {}).update(members)
        self.commands.clear()


def session(sid, token, minutes=60):
    return {"sid": sid, "user_id": 7, "refresh_token_hash": token,
            "expires_at": (BASE + timedelta(minutes=minutes)).isoformat()}


def event(table, op, before=None, after=None):
    return {"source": {"table": table}, "op": op, "before": before, "after": after}


class AuthCdcTests(unittest.TestCase):
    def setUp(self):
        self.redis = MemoryRedis()

    def apply(self, value):
        module.apply_change(self.redis, value, PREFIX, "Asia/Seoul", NOW)

    def test_snapshot_rotation_duplicate_and_logout(self):
        first = session("sid-1", "old")
        latest = session("sid-1", "new")
        self.apply(event("login_sessions", "r", after=first))
        rotated = event("login_sessions", "u", before=first, after=latest)
        self.apply(rotated)
        self.apply(rotated)
        self.assertNotIn(PREFIX + "refresh:old", self.redis.data)
        self.assertEqual("sid-1", self.redis.data[PREFIX + "refresh:new"])
        self.assertEqual("new", self.redis.data[PREFIX + "session:sid-1"]["refreshTokenHash"])
        self.apply(event("login_sessions", "d", before=latest))
        self.apply(event("login_sessions", "d", before=latest))
        self.assertFalse(any(key.startswith(PREFIX) for key in self.redis.data))

    def test_user_index_keeps_latest_member_expiry(self):
        self.apply(event("login_sessions", "r", after=session("late", "hash-late", 90)))
        self.apply(event("login_sessions", "r", after=session("early", "hash-early", 30)))
        index = PREFIX + "user-sessions:7"
        self.assertEqual(self.redis.data[index]["late"], self.redis.deadlines[index])

    def test_csrf_update_expiry_and_delete(self):
        first = {"browser_context_hash": "context", "token_value": "one",
                 "expires_at": (BASE + timedelta(minutes=5)).isoformat()}
        later = dict(first, token_value="two")
        self.apply(event("csrf_tokens", "r", after=first))
        self.apply(event("csrf_tokens", "u", before=first, after=later))
        self.assertEqual("two", self.redis.data[PREFIX + "csrf:context"])
        self.apply(event("csrf_tokens", "d", before=later))
        self.assertNotIn(PREFIX + "csrf:context", self.redis.data)

    def test_microtimestamp_is_mysql_wall_time_in_seoul(self):
        microseconds = int((BASE - datetime(1970, 1, 1)).total_seconds() * 1_000_000)
        self.assertEqual(module._expiry_ms(microseconds, "Asia/Seoul"), NOW)

    def test_rejects_unknown_events_and_shared_prefix(self):
        with self.assertRaises(ValueError):
            self.apply(event("users", "r", after=session("sid", "hash")))
        with self.assertRaises(ValueError):
            module.apply_change(self.redis, event("login_sessions", "r", after=session("sid", "hash")),
                                "yeodam:v2:staging:auth:", "Asia/Seoul", NOW)

    def test_encrypted_topic_authentication(self):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            self.skipTest("cryptography is not installed in this Gate runner")
        topic = "staging.yeodam.migration.auth368.encrypted"
        key = bytes(range(32))
        payload = event("login_sessions", "r", after=session("sid", "hash"))
        nonce = bytes(range(12))
        ciphertext = AESGCM(key).encrypt(nonce, json.dumps(payload).encode(), topic.encode())
        record = b"v1:" + base64.b64encode(nonce + ciphertext)
        self.assertEqual(module.decrypt_event(record, topic, key), payload)
        with self.assertRaises(ValueError):
            module.decrypt_event(json.dumps(payload).encode(), topic, key)
        with self.assertRaises(Exception):
            module.decrypt_event(record, "staging.yeodam.migration.auth368.other", key)


if __name__ == "__main__":
    unittest.main()
