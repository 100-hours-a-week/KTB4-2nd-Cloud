"""Actual local HTTP barriers and pinned auth representation, never live services."""
import asyncio
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from request_cutover.auth import expiry, fingerprint, project
from request_cutover.config import load

HAS_HTTP = importlib.util.find_spec('aiohttp') is not None
if HAS_HTTP:
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from request_cutover.gate import Gate, applications


def fixture_rows():
    until = '2030-01-01T00:00:00'
    return {'sessions': [dict(sid='sid-a', user_id=1, refresh_token_hash='a' * 64,
                             expires_at=until),
                         dict(sid='sid-b', user_id=1, refresh_token_hash='b' * 64,
                              expires_at='2030-01-02T00:00:00')],
        'csrf': [dict(browser_context_hash='c' * 64, token_value='fixture-csrf', expires_at=until)],
        'state': [dict(state_hash='d' * 64, browser_context_hash='c' * 64, expires_at=until)],
        'ticket': [dict(ticket_hash='e' * 64, browser_context_hash='c' * 64,
                       provider_user_id='123', email='fixture@staging.invalid',
                       profile_image_url=None, expires_at=until)],
        'profile': [dict(token_hash='f' * 64, provider_user_id='123', email='fixture@staging.invalid',
                        profile_image_url=None, expires_at=until)]}


class Representation(unittest.TestCase):
    def test_absolute_expiry_reverse_index_and_max_user_index(self):
        rows = fixture_rows()
        values = project(rows, 'run:', 'UTC', 0, 'fixture-owner')
        end = expiry(rows['sessions'][0]['expires_at'], 'UTC')
        self.assertEqual(values['run:session:sid-a'].value['expiresAt'], str(end))
        self.assertEqual(values['run:refresh:' + 'a' * 64].value, 'sid-a')
        self.assertEqual(values['run:user-sessions:1'].expires,
                         expiry('2030-01-02T00:00:00', 'UTC'))
        token = json.loads(values['run:login-ticket:' + 'e' * 64].value)
        self.assertIsNone(token['claimOwner'])
        self.assertEqual(token['browserContextHash'], 'c' * 64)
        self.assertIsNone(json.loads(values['run:profile-token:' + 'f' * 64].value)['browserContextHash'])

    def test_expired_items_do_not_resurrect(self):
        self.assertEqual(project(fixture_rows(), 'run:', 'UTC', 9999999999999, 'owner'), {})

    def test_timezone_is_not_assumed(self):
        self.assertEqual(expiry('2030-01-01T09:00:00', 'Asia/Seoul'),
                         expiry('2030-01-01T00:00:00', 'UTC'))

    def test_duplicate_refresh_hash_rejected(self):
        rows = fixture_rows()
        rows['sessions'][1]['refresh_token_hash'] = 'a' * 64
        with self.assertRaises(ValueError):
            project(rows, 'run:', 'UTC', 0, 'owner')

    def test_no_shared_database_or_prefix(self):
        config = json.loads((ROOT / 'deploy/request-cutover/config.example.json').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            for field, value in [('database', 'yeodam_validation'), ('database', 'yeodam_v1_source')]:
                config['mysql'][field] = value
                path.write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    load(path)

    def test_size_backfill_uses_head_length_and_conditional_update(self):
        from request_cutover.sizes import backfill
        from unittest.mock import MagicMock
        db, s3 = MagicMock(), MagicMock()
        cursor = db.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [{'file_id': 7, 'object_key': 'fixture.png'}]
        cursor.rowcount = 1
        s3.head_object.return_value = {'ContentLength': 1234}
        self.assertEqual(backfill(db, s3, 'fixture', True)['updated'], 1)
        sql, values = cursor.execute.call_args.args
        self.assertIn('original_size_bytes IS NULL', sql)
        self.assertEqual(values, (1234, 7, 'fixture.png'))
        self.assertEqual(s3.head_object.call_args.kwargs, {'Bucket': 'fixture', 'Key': 'fixture.png'})



@unittest.skipUnless(HAS_HTTP, 'Run HTTP tests with deploy/request-cutover/requirements.txt')
class Barrier(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        self.slow_entered, self.slow_release = asyncio.Event(), asyncio.Event()

        async def upstream(request):
            body = await request.read()
            self.calls.append((request.app['version'], request.method, str(request.rel_url), body,
                               request.headers.get('Cookie'), request.headers.get('X-CSRF-TOKEN')))
            if request.path == '/api/slow':
                self.slow_entered.set()
                await self.slow_release.wait()
            if request.path == '/api/drop':
                request.transport.close()
                return web.Response()
            response = web.Response(body=b'payload\x00unchanged', status=201)
            response.headers.add('Set-Cookie', 'a=1; HttpOnly; Path=/')
            response.headers.add('Set-Cookie', 'b=2; Secure; Path=/')
            return response

        self.upstreams = []
        for version in ('V1', 'V2'):
            app = web.Application()
            app['version'] = version
            app.router.add_route('*', '/{p:.*}', upstream)
            server = TestServer(app)
            await server.start_server()
            self.upstreams.append(server)
        self.config = json.loads((ROOT / 'deploy/request-cutover/config.example.json').read_text())
        self.config.update(v1_api=str(self.upstreams[0].make_url('')).rstrip('/'),
                           v1_web=str(self.upstreams[0].make_url('')).rstrip('/'),
                           v2_origin=str(self.upstreams[1].make_url('')).rstrip('/'),
                           journal=self.tmp.name + '/journal.json', hold_seconds=1)
        app, admin = await applications(self.config)
        self.gate = app['gate']
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        self.slow_release.set()
        await self.client.close()
        for server in self.upstreams:
            await server.close()
        self.tmp.cleanup()

    def proof(self, hold):
        return {'hold_token': hold['hold_token'], 'run_id': self.config['run_id'],
                'pending_jobs': 0, 'keys': 0,
                'source_fingerprint': fingerprint({}), 'target_fingerprint': fingerprint({})}

    async def test_hold_waits_then_delivers_post_once_with_headers_and_cookies(self):
        hold = await self.gate.begin()
        task = asyncio.create_task(self.client.post('/api/probe?x=a%2Fb', data=b'body\x00exact',
            headers={'Cookie': 'accessToken=fixture', 'X-CSRF-TOKEN': 'fixture'}))
        await asyncio.sleep(0.03)
        self.assertEqual(self.calls, [])
        await self.gate.commit(self.proof(hold))
        response = await task
        self.assertEqual(await response.read(), b'payload\x00unchanged')
        self.assertEqual(response.status, 201)
        self.assertEqual(len(response.headers.getall('Set-Cookie')), 2)
        self.assertEqual(self.calls, [('V2', 'POST', '/api/probe?x=a/b', b'body\x00exact',
                                      'accessToken=fixture', 'fixture')])
        with self.assertRaises(ValueError):
            async with self.gate.changed:
                self.gate.abort_locked('late')
        self.assertEqual(Gate(self.config).phase, 'V2')

    async def test_existing_v1_request_must_finish_before_commit(self):
        first = asyncio.create_task(self.client.get('/api/slow'))
        await self.slow_entered.wait()
        hold = await self.gate.begin()
        with self.assertRaises(ValueError):
            await self.gate.commit(self.proof(hold))
        self.slow_release.set()
        await (await first).read()
        await self.gate.commit(self.proof(hold))

    async def test_timeout_releases_pending_request_to_v1_without_retry(self):
        self.gate.config['hold_seconds'] = 0.05
        await self.gate.begin()
        response = await self.client.post('/api/probe', data=b'once')
        await response.read()
        self.assertEqual([x[0] for x in self.calls], ['V1'])
        self.assertEqual(self.gate.reason, 'hold_timeout')

    async def test_sealed_hold_timeout_never_reopens_v1(self):
        self.gate.config['hold_seconds'] = 0.05
        hold = await self.gate.begin()
        await self.gate.seal(hold['hold_token'], self.proof(hold))
        response = await self.client.post('/api/probe', data=b'once')
        await response.read()
        self.assertEqual(response.status, 502)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.gate.phase, 'BLOCKED')
        self.gate.config['hold_seconds'] = 1
        self.assertEqual(Gate(self.config).phase, 'BLOCKED')
        with self.assertRaises(ValueError):
            async with self.gate.changed:
                self.gate.abort_locked('unsafe rollback')

    async def test_db_handoff_mode_requires_seal(self):
        self.gate.config['require_seal'] = True
        hold = await self.gate.begin()
        with self.assertRaisesRegex(ValueError, 'sealed HTTP gate'):
            await self.gate.commit(self.proof(hold))
        await self.gate.seal(hold['hold_token'], self.proof(hold))
        self.assertTrue(self.gate.sealed)
        with self.assertRaises(KeyError):
            await self.gate.commit(self.proof(hold))

    async def test_sealed_commit_and_abort_follow_db_journal(self):
        self.gate.config['require_seal'] = True
        self.gate.config['a_db_target'] = '10.30.10.28'
        self.gate.config['a_db_journal'] = self.tmp.name + '/db-handoff.json'
        hold = await self.gate.begin()
        await self.gate.seal(hold['hold_token'], self.proof(hold))
        journal = Path(self.config['a_db_journal'])
        journal.write_text(json.dumps({'runId': self.config['run_id'], 'target': '10.30.10.28',
            'phase': 'aborted', 'promotionPossible': False}))
        journal.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'required phase'):
            await self.gate.commit(self.proof(hold))
        self.assertEqual((await self.gate.resume_v1(hold['hold_token']))['phase'], 'V1')
        hold = await self.gate.begin()
        await self.gate.seal(hold['hold_token'], self.proof(hold))
        journal.write_text(json.dumps({'runId': self.config['run_id'], 'target': '10.30.10.28',
            'phase': 'switched', 'promotionPossible': True}))
        self.assertEqual((await self.gate.commit(self.proof(hold)))['phase'], 'V2')

    async def test_expired_a_hold_preserves_failure_then_recovers_forward(self):
        self.gate.config.update(require_seal=True, hold_seconds=0.05,
            a_db_target='10.30.10.28', a_db_journal=self.tmp.name + '/db-handoff.json')
        hold = await self.gate.begin()
        proof = self.proof(hold)
        await self.gate.seal(hold['hold_token'], proof)
        await asyncio.sleep(0.07)
        self.assertEqual(self.gate.phase, 'BLOCKED')
        journal = Path(self.config['a_db_journal'])
        journal.write_text(json.dumps({'runId': self.config['run_id'], 'target': '10.30.10.28',
            'phase': 'switched', 'promotionPossible': True}))
        journal.chmod(0o600)
        wrong = dict(proof, target_fingerprint='0' * 64)
        with self.assertRaisesRegex(ValueError, 'sealed source snapshot'):
            await self.gate.recover_v2(wrong)
        self.assertEqual((await self.gate.recover_v2(proof))['phase'], 'V2')
        self.assertEqual(self.gate.reason, 'recovered_after_failed_hold')
        self.assertEqual(json.loads(Path(self.config['journal']).read_text())['phase'], 'V2')

    async def test_a_controller_never_commits_before_db_switch(self):
        from request_cutover.control import _a_cutover
        from unittest.mock import MagicMock, AsyncMock
        import request_cutover.control as control
        self.gate.config.update(require_seal=True, a_db_target='10.30.10.28',
            a_db_journal=self.tmp.name + '/db-handoff.json')
        journal = Path(self.config['a_db_journal'])
        sequence = []

        async def local_admin(config, command='status', payload=None):
            sequence.append(command)
            if command == 'status':
                return self.gate.status()
            if command == 'begin':
                return await self.gate.begin()
            if command == 'seal':
                return await self.gate.seal(payload['hold_token'], payload)
            if command == 'commit':
                return await self.gate.commit({'run_id': config['run_id'], **payload})
            if command == 'resume-v1':
                return await self.gate.resume_v1(payload['hold_token'])
            async with self.gate.changed:
                self.gate.abort_locked('controller_abort')
            return self.gate.status()

        def db_check():
            sequence.append('db_check')

        def db_switch():
            sequence.append('db_switch')
            journal.write_text(json.dumps({'runId': self.config['run_id'],
                'target': '10.30.10.28', 'phase': 'switched', 'promotionPossible': True}))
            journal.chmod(0o600)

        def auth_transfer(config):
            sequence.append('auth_snapshot')
            return {'pending_jobs': 0, 'keys': 0,
                'source_fingerprint': fingerprint({}), 'target_fingerprint': fingerprint({})}

        with patch.object(control, 'admin', local_admin), \
                patch.object(control, 'database', return_value=MagicMock()), \
                patch.object(control, 'checks'), \
                patch.object(control, 'transfer', auth_transfer), \
                patch.object(control, 'healthy', new_callable=AsyncMock):
            result = await _a_cutover(self.config, db_check, db_switch)
        self.assertEqual(result['phase'], 'V2')
        self.assertLess(sequence.index('auth_snapshot'), sequence.index('seal'))
        self.assertLess(sequence.index('seal'), sequence.index('db_switch'))
        self.assertLess(sequence.index('db_switch'), sequence.index('commit'))

    async def test_capacity_aborts_instead_of_rejecting_request(self):
        self.gate.config['max_wait_bytes'] = 1
        await self.gate.begin()
        response = await self.client.post('/api/probe', data=b'two')
        await response.read()
        self.assertEqual(response.status, 201)
        self.assertEqual(self.gate.reason, 'hold_capacity')

    async def test_multipart_aborts_before_moving_legacy_batch(self):
        await self.gate.begin()
        response = await self.client.post('/api/initial-attachments', data=b'fixture',
            headers={'Content-Type': 'multipart/form-data; boundary=fixture'})
        await response.read()
        self.assertEqual(self.calls[0][0], 'V1')
        self.assertEqual(self.gate.reason, 'legacy_multipart_arrived')

    async def test_side_effecting_get_is_not_retried(self):
        response = await self.client.get('/api/drop')
        self.assertEqual(response.status, 502)
        await response.read()
        self.assertEqual(len(self.calls), 1)

    async def test_encoded_path_and_query_are_preserved(self):
        from yarl import URL
        response = await self.client.get(URL('/api/probe%2Fpart?x=a%2Fb', encoded=True))
        await response.read()
        self.assertEqual(self.calls[0][2], '/api/probe%2Fpart?x=a%2Fb')

    async def test_uncertain_journal_cannot_resume_v1(self):
        hold = await self.gate.begin()
        with patch.object(self.gate, 'persist', side_effect=OSError('fixture fsync failure')):
            with self.assertRaises(OSError):
                await self.gate.commit(self.proof(hold))
        self.assertEqual(self.gate.phase, 'BLOCKED')
        async with self.gate.changed:
            with self.assertRaises(ValueError):
                self.gate.abort_locked('unsafe rollback')
        response = await self.client.get('/api/probe')
        await response.read()
        self.assertEqual(response.status, 502)
        self.assertFalse(self.calls)


    async def test_overlapping_controller_cannot_import(self):
        import fcntl
        from request_cutover.control import cutover
        with open(self.config['journal'] + '.controller.lock', 'w') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'another controller'):
                await cutover(self.config)


    async def test_metrics_exporter_cannot_execute_control_commands(self):
        from request_cutover.exporter import application
        _, admin = await applications(self.config)
        runner = web.AppRunner(admin, access_log=None)
        await runner.setup()
        socket = self.tmp.name + '/metrics-control.sock'
        await web.UnixSite(runner, socket).start()
        async with TestClient(TestServer(application(socket))) as client:
            response = await client.get('/metrics')
            self.assertIn('yeodam_request_cutover_exporter_up 1', await response.text())
            response = await client.post('/begin', json={'run_id': self.config['run_id']})
            self.assertEqual(response.status, 404)
            await response.read()
            self.assertEqual(self.gate.phase, 'V1')
        await runner.cleanup()


    async def test_bad_or_superseded_proof_cannot_commit(self):
        hold = await self.gate.begin()
        wrong = self.proof(hold)
        wrong['hold_token'] = 'old'
        with self.assertRaises(ValueError):
            await self.gate.commit(wrong)
        wrong = self.proof(hold)
        wrong['target_fingerprint'] = '0' * 64
        with self.assertRaises(ValueError):
            await self.gate.commit(wrong)


if __name__ == '__main__':
    unittest.main()
