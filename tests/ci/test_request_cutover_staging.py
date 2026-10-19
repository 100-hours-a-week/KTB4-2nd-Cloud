"""Staging scope, read-only diagnostics and deferred host lifecycle safety."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from request_cutover.config import load
from request_cutover.hostops import operation_lock, require_detached, require_ready, verify_bundle
from request_cutover.staging import ACCOUNT, HOST, SOURCE, VPC, gate_config, read_json, render, signature, validate
from request_cutover.staging_preflight import evaluate, host_probes, mysql_probe, redis_probe
from request_cutover.gate import Gate


def plan_fixture():
    return read_json(ROOT / 'deploy/request-cutover/staging/plan.example.json')


def facts_fixture(plan, now):
    origin = 'https://' + HOST
    return {'run_id': plan['run_id'], 'plan_hash': signature(plan), 'observed_at': now.isoformat(),
      'account_id': ACCOUNT,
      'source': {'id': SOURCE, 'ip': plan['source']['private_ip'], 'vpc': VPC, 'state': 'running'},
      'alb': {'dns': plan['target']['alb_dns_name'], 'vpc': VPC, 'certificate_names': [plan['target']['tls_name']]},
      'backend': {'service': plan['target']['service'], 'running': 1, 'desired': 1, 'target_health': ['healthy'],
        'env': {'MYSQL_URL': 'jdbc:mysql://10.30.10.28:3306/yeodam_v1_source?serverTimezone=UTC',
          'JWT_ISSUER': origin, 'FRONTEND_ORIGIN': origin,
          'FRONTEND_OAUTH_CALLBACK_URI': origin + '/auth/callback',
          'KAKAO_REDIRECT_URI': origin + '/api/auth/kakao/callback',
          'REDIS_HOST': plan['redis']['host'], 'REDIS_PORT': '6379', 'REDIS_SSL_ENABLED': 'true',
          'AUTH_SESSION_KEY_PREFIX': plan['redis']['prefix']}}}


class Preparation(unittest.TestCase):
    def setUp(self):
        self.plan = plan_fixture()
        self.now = datetime.now(timezone.utc)

    def test_inventory_does_not_export_secret_fields_or_jdbc_credentials(self):
        from request_cutover.staging import public_environment
        for uri in ('jdbc:mysql://user:private-fixture@host/db',
                    'jdbc:mysql://host/db?password=private-fixture'):
            data = public_environment([{'name':'MYSQL_URL','value':uri},
                {'name':'JWT_SECRET','value':'private-fixture'}, {'name':'REDIS_PASSWORD','value':'private-fixture'}])
            self.assertNotIn('private-fixture', json.dumps(data))
            self.assertEqual(set(data), {'MYSQL_URL'})


    def test_production_qa_schema_tls_and_injection_rejected(self):
        for section, key, value in [('source','instance_id','i-051aaa0e3462d2da7'),
                ('source','public_host','yeodam-2gether.com'),
                ('mysql','database','yeodam_validation'),('mysql','target_host','10.30.10.207'),
                ('redis','tls',False),('tls','certificate','/tmp/a;return 200;')]:
            candidate = deepcopy(self.plan)
            candidate[section][key] = value
            with self.subTest(section=section, key=key), self.assertRaises(ValueError):
                validate(candidate)

    def test_bundle_render_integrity_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'bundle'
            render(self.plan, 'a' * 40, folder)
            verify_bundle(folder)
            config = load(folder / 'gate.json')
            self.assertTrue(config['preparation_only'])
            self.assertEqual(config['v2_tls_name'], 'staging.yeodam-2gether.com')
            self.assertEqual(folder.stat().st_mode & 0o077, 0)
            with self.assertRaises(FileExistsError):
                render(self.plan, 'a' * 40, folder)
            (folder / 'nginx.candidate.conf').write_text('unexpected configuration')
            with self.assertRaisesRegex(ValueError, 'content changed'):
                verify_bundle(folder)

    def test_preparation_cannot_hold_or_resume_v2_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = gate_config(self.plan)
            config['journal'] = str(Path(tmp) / 'journal.json')
            gate = Gate(config)
            with self.assertRaisesRegex(ValueError, 'cannot HOLD'):
                asyncio.run(gate.begin())
            self.assertEqual(gate.phase, 'V1')
            gate.persist('V2')
            with self.assertRaisesRegex(ValueError, 'cannot resume'):
                Gate(config)

    def test_current_qa_contract_is_blocked_without_mutation(self):
        facts = facts_fixture(self.plan, self.now)
        facts['backend']['env'].update(MYSQL_URL='jdbc:mysql://mysql-validation.staging.yeodam.internal:3306/yeodam_validation',
            JWT_ISSUER='https://staging.yeodam-2gether.com', AUTH_SESSION_KEY_PREFIX='yeodam:v2:staging:auth:')
        report = evaluate(self.plan, facts, now=self.now)
        self.assertEqual(report['result'], 'blocked')
        self.assertTrue({'db_test_handoff','v2_uses_migration_database','v2_public_contract','v2_redis_contract'} <= set(report['blocked']))
        self.assertFalse(report['activation_allowed'])

    def test_offline_pass_still_cannot_install_and_stale_inventory_fails(self):
        self.plan['handoff'] = {'db_tests_complete': True, 'evidence': 'local-record:DB-release'}
        self.plan['mysql']['expected_uuid'] = '22222222-2222-4222-a222-222222222222'
        facts = facts_fixture(self.plan, self.now)
        report = evaluate(self.plan, facts, now=self.now)
        self.assertEqual(report['result'], 'pass')
        with self.assertRaises(ValueError):
            require_ready(self.plan, report, now=self.now)
        stale = evaluate(self.plan, facts, now=self.now + timedelta(minutes=6))
        self.assertIn('fresh_matching_inventory', stale['blocked'])
        facts['run_id'] = '33333333-3333-4333-a333-333333333333'
        self.assertIn('fresh_matching_inventory', evaluate(self.plan, facts, now=self.now)['blocked'])

    def test_wrong_host_or_unreleased_db_never_gets_network_probes(self):
        with patch('request_cutover.staging_preflight.metadata', return_value={'id': 'production', 'ip': '10.20.1.110'}), \
                patch('request_cutover.staging_preflight.mysql_probe') as db:
            host_probes(self.plan, {'CUTOVER_MYSQL_PASSWORD': 'private-fixture'})
            db.assert_not_called()
        with patch('request_cutover.staging_preflight.metadata', return_value={'id': SOURCE, 'ip': self.plan['source']['private_ip']}), \
                patch('request_cutover.staging_preflight.mysql_probe') as db:
            host_probes(self.plan, {'CUTOVER_MYSQL_PASSWORD': 'private-fixture'})
            db.assert_not_called()

    def test_mysql_uses_read_only_transaction_select_and_rollback(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        row = {'uuid': 'fixture-writer', 'ro': 0, 'sro': 0}
        cursor.fetchone.side_effect = [dict(row), {'n': 0}, {'n': 0}, dict(row), {'n': 0}, {'n': 0}]
        columns = [{'column_name': 'original_size_bytes'}]
        versions = [{'version': str(v)} for v in range(1, 26)]
        cursor.fetchall.side_effect = [versions, columns, versions, columns]
        with patch('pymysql.connect', return_value=connection):
            result = mysql_probe(self.plan, 'private-fixture')
        self.assertTrue(result['schema_ready'])
        self.assertEqual(connection.rollback.call_count, 2)
        for call in cursor.execute.call_args_list:
            sql = call.args[0].strip().upper()
            self.assertTrue(sql == 'START TRANSACTION READ ONLY' or sql.startswith('SELECT '))

    def test_redis_probe_only_reads_and_uses_tls(self):
        client = MagicMock()
        client.info.return_value = {'role': 'master'}
        client.scan.return_value = (0, [])
        factory = MagicMock()
        factory.return_value.__enter__.return_value = client
        with patch('redis.Redis', factory):
            self.assertEqual(redis_probe(self.plan, 'private-fixture'), {'master': True, 'empty': True})
        self.assertTrue(factory.call_args.kwargs['ssl'])
        self.assertEqual([c[0] for c in client.method_calls], ['ping','info','scan'])

    def test_stop_rejects_loaded_nginx_inflight_or_committed_authority(self):
        state = {'phase':'V1','waiters':0,'inflight':{'V1':0,'V2':0}}
        require_detached('proxy_pass http://127.0.0.1:8080;', state)
        for dump, candidate in [('proxy_pass http://127.0.0.1:19091;', state),
            ('', {**state,'phase':'V2'}), ('', {**state,'inflight':{'V1':1,'V2':0}}),
            ('', {**state,'inflight':{}})]:
            with self.assertRaises(ValueError):
                require_detached(dump, candidate)

    def test_duplicate_host_operation_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with operation_lock(Path(tmp) / 'lock'):
                with self.assertRaises(ValueError):
                    with operation_lock(Path(tmp) / 'lock'):
                        self.fail('overlapping operation accepted')


if __name__ == '__main__':
    unittest.main()
