import base64
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from datetime import datetime, timezone


script = Path(__file__).parents[1] / "scripts" / "synthetic-session-pool.py"
spec = importlib.util.spec_from_file_location("synthetic_pool", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SyntheticSessionPoolTest(unittest.TestCase):
    def test_jwt_claims_and_signature_match_v1_contract(self):
        now = datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc)
        secret = "s" * 32
        token = module.access_token(42, "sid-1", secret, "https://v1-staging.example", now)
        header, claims, signature = token.split(".")
        decode = lambda part: base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        self.assertEqual(json.loads(decode(header)), {"alg": "HS256"})
        self.assertEqual(json.loads(decode(claims)), {
            "iss": "https://v1-staging.example", "aud": ["yeodam-api"],
            "iat": 1791936000, "exp": 1791937800, "sub": "42", "sid": "sid-1",
        })
        expected = base64.urlsafe_b64encode(
            hmac.new(secret.encode(), (header + "." + claims).encode(), hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        self.assertEqual(signature, expected)

    def test_sql_literal_rejects_unexpected_input(self):
        self.assertEqual(module.sql_literal("mig-abc%@staging.invalid"), "'mig-abc%@staging.invalid'")
        for value in ("' OR 1=1", "abc_123", "한글", "a;b"):
            with self.assertRaises(ValueError):
                module.sql_literal(value)

    def test_prepare_writes_private_pool_without_printing_tokens(self):
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary) / "private"
            output = private / "pool.json"
            queries = []

            def fake_mysql(statement):
                queries.append(statement)
                if statement.startswith("SELECT DATABASE()"):
                    return module.SCHEMA + "\thost"
                if statement.startswith("SELECT COUNT(*)"):
                    return "0"
                return "42"

            args = SimpleNamespace(run_id="12345678-1234-1234-1234-123456789abc", count=1, output=str(output))
            with patch.object(module.os, "geteuid", return_value=0), \
                 patch.object(module, "run_mysql", side_effect=fake_mysql), \
                 patch.object(module, "parameter", side_effect=["s" * 32, "https://v1-staging.example"]), \
                 patch.object(module, "http_session", return_value="csrf-value"), \
                 patch("builtins.print") as printed:
                module.prepare(args)
            pool = json.loads(output.read_text())
            self.assertEqual(len(pool["sessions"]), 1)
            self.assertEqual(pool["sessions"][0]["userId"], 42)
            self.assertEqual(pool["sessions"][0]["csrfToken"], "csrf-value")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(private.stat().st_mode & 0o777, 0o700)
            self.assertNotIn(pool["sessions"][0]["accessToken"], str(printed.call_args_list))
            self.assertTrue(any("INSERT INTO login_sessions" in query for query in queries))

    def test_cleanup_uses_run_scoped_email_without_self_referencing_delete(self):
        run_id = "12345678-1234-1234-1234-123456789abc"
        statements = []

        def fake_mysql(statement):
            statements.append(statement)
            if statement.startswith("SELECT sid"):
                return "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            if statement.startswith("SELECT COUNT"):
                return "1"
            return ""

        with patch.object(module.os, "geteuid", return_value=0), \
             patch.object(module, "run_mysql", side_effect=fake_mysql), \
             patch("builtins.print"):
            module.cleanup(SimpleNamespace(run_id=run_id, output=None))
        self.assertEqual(statements[-1],
                         "DELETE FROM users WHERE email LIKE 'mig-" + run_id + "-%@staging.invalid';")
        self.assertTrue(any("DELETE FROM csrf_tokens" in statement for statement in statements))

    def test_existing_user_pool_preserves_seeded_users_and_scopes_cleanup(self):
        dataset = "c0014ce1-0398-4a0c-95de-f502cb2e412e"
        run_id = "12345678-1234-1234-1234-123456789abc"
        statements = []

        def fake_mysql(statement):
            statements.append(statement)
            if statement.startswith("SELECT DATABASE()"):
                return module.SCHEMA + "\thost"
            if statement.startswith("SELECT @@read_only"):
                return "0\t0"
            if statement.startswith("SELECT user_id FROM users"):
                return "42"
            if statement.startswith("SELECT COUNT(*) FROM login_sessions"):
                return "1"
            return ""

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "private" / "pool.json"
            with patch.object(module.os, "geteuid", return_value=0), \
                 patch.object(module, "run_mysql", side_effect=fake_mysql), \
                 patch.object(module, "parameter", side_effect=["s" * 32, "https://v1-staging.example"]), \
                 patch.object(module, "http_session", return_value="csrf-value"):
                module.prepare_existing(SimpleNamespace(run_id=run_id, dataset_id=dataset, count=1, output=str(output)))
                pool = json.loads(output.read_text())
                self.assertEqual(pool["mode"], "existing")
                self.assertEqual(pool["sessions"][0]["userId"], 42)
                self.assertEqual(pool["sessions"][0]["csrfToken"], "csrf-value")
                refresh_token = pool["sessions"][0]["refreshToken"]
                self.assertTrue(refresh_token)
                self.assertTrue(any(
                    "'" + module.hashlib.sha256(refresh_token.encode()).hexdigest() + "'" in sql
                    for sql in statements if sql.startswith("INSERT INTO login_sessions")
                ))
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                with self.assertRaisesRegex(RuntimeError, "cleanup-existing"):
                    module.cleanup(SimpleNamespace(run_id=run_id, output=str(output)))
                module.cleanup_existing(SimpleNamespace(run_id=run_id, dataset_id=dataset, output=str(output)))
            self.assertFalse(output.exists())
        self.assertTrue(any("mig-data-" + dataset.replace("-", "") + "-00000@staging.invalid" in sql
                            for sql in statements))
        self.assertFalse(any("INSERT INTO users" in sql or "DELETE FROM users" in sql for sql in statements))
        self.assertTrue(any("DELETE FROM login_sessions WHERE sid=" in sql for sql in statements))

    def test_existing_user_partial_insert_failure_keeps_cleanup_checkpoint(self):
        dataset = "c0014ce1-0398-4a0c-95de-f502cb2e412e"
        run_id = "12345678-1234-1234-1234-123456789abc"

        def fake_mysql(statement):
            if statement.startswith("SELECT DATABASE()"):
                return module.SCHEMA + "\thost"
            if statement.startswith("SELECT @@read_only"):
                return "0\t0"
            if statement.startswith("SELECT user_id FROM users"):
                return "42"
            if statement.startswith("INSERT INTO login_sessions"):
                raise RuntimeError("simulated insert failure")
            if statement.startswith("SELECT COUNT(*) FROM login_sessions"):
                return "0"
            return ""

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "private" / "pool.json"
            with patch.object(module.os, "geteuid", return_value=0), \
                 patch.object(module, "run_mysql", side_effect=fake_mysql), \
                 patch.object(module, "parameter", side_effect=["s" * 32, "https://v1-staging.example"]), \
                 patch.object(module, "http_session") as http_smoke:
                with self.assertRaisesRegex(RuntimeError, "simulated insert failure"):
                    module.prepare_existing(SimpleNamespace(run_id=run_id, dataset_id=dataset, count=1,
                                                            output=str(output)))
                self.assertEqual(len(json.loads(output.read_text())["sessions"]), 1)
                module.cleanup_existing(SimpleNamespace(run_id=run_id, dataset_id=dataset, output=str(output)))
                http_smoke.assert_not_called()
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
