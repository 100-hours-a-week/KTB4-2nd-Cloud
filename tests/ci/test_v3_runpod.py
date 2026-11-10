"""Concurrent URL rotation, recovery and real async drain tests; synthetic data only."""
import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import v3_runpod_update as update


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


class Secrets:
    def __init__(self):
        self.version = 'a' * 32
        self.values = {self.version: {'VLM_BASE_URL': 'https://old-8000.proxy.runpod.net/v1',
                                    'VLM_API_KEY': 'fixture-secret-never-report', 'REDIS_URL': 'fixture-preserve'}}
        self.moves = []
        self.lost = False
        self.foreign = False
    def current(self): return self.version, copy.deepcopy(self.values[self.version])
    def stage(self, token, data): self.values[token] = copy.deepcopy(data)
    def move(self, new, previous):
        if self.foreign:
            self.version = 'b' * 32; self.values[self.version] = copy.deepcopy(self.values[previous]); raise OSError('lost response')
        if previous != self.version: raise ValueError('foreign owner')
        self.version = new; self.moves.append((new, previous))
        if self.lost:
            self.lost = False; raise OSError('response lost after successful AWS change')


class Cluster:
    def __init__(self): self.calls = []; self.fail = None; self.drained = False
    def preflight(self, _): self.calls.append('preflight')
    def drain(self, _): self.calls.append('drain'); self.drained = True
    def assert_idle(self, _):
        if not self.drained: raise ValueError('busy')
    def sync(self, _): self.calls.append('sync')
    def replace(self, name, op, value):
        self.calls.append('replace:' + name)
        if self.fail == name:
            self.fail = None; raise ValueError('fixture Pod failure')
    def complete(self): self.calls.append('complete')
    def resume(self, _): self.calls.append('resume'); self.drained = False


class Rotation(unittest.TestCase):
    def setUp(self): self.s = Secrets(); self.k = Cluster(); self.reports = []
    def run_update(self, url='https://new-8000.proxy.runpod.net/v1', probe=lambda *_: None):
        return update.perform(url, self.s, self.k, probe, lambda r: self.reports.append(copy.deepcopy(r)))
    def test_success_updates_one_field_and_both_roles(self):
        result = self.run_update(); self.assertTrue(result['completed'])
        self.assertEqual(self.s.current()[1]['REDIS_URL'], 'fixture-preserve')
        self.assertEqual(self.k.calls[-4:], ['replace:ai-runner', 'replace:ai-api', 'complete', 'resume'])
        self.assertEqual(len(self.s.moves), 1)
    def test_public_receipt_contains_no_credentials_or_urls(self):
        self.run_update(); output = json.dumps(self.reports)
        for value in ['fixture-secret-never-report', 'fixture-preserve', 'https://']:
            self.assertNotIn(value, output)
    def test_invalid_endpoint_rejected_before_access(self):
        for url in ['http://new-8000.proxy.runpod.net/v1', 'https://attacker.example/v1',
                    'https://new-8000.proxy.runpod.net/v1?key=bad', 'https://x@new-8000.proxy.runpod.net/v1',
                    'https://new-8000.proxy.runpod.net/v1\n']:
            with self.subTest(url=url), self.assertRaises(ValueError): self.run_update(url)
        self.assertFalse(self.k.calls)
    def test_model_probe_failure_never_drains_or_changes_secret(self):
        with self.assertRaises(OSError): self.run_update(probe=lambda *_: (_ for _ in ()).throw(OSError()))
        self.assertEqual(self.k.calls, ['preflight']); self.assertFalse(self.s.moves)
    def test_same_url_is_verified_without_replacement(self):
        r = self.run_update(self.s.current()[1]['VLM_BASE_URL'])
        self.assertEqual(r['phase'], 'unchanged'); self.assertFalse(self.s.moves)
    def test_busy_never_promotes_or_kills(self):
        def busy(_): raise ValueError('busy timeout')
        self.k.drain = busy
        with self.assertRaises(RuntimeError): self.run_update()
        self.assertFalse(self.s.moves); self.assertEqual(self.k.calls[-1], 'resume')
    def test_restart_failure_recovers_original_version(self):
        self.k.fail = 'ai-api'
        with self.assertRaises(RuntimeError): self.run_update()
        self.assertEqual(self.s.version, 'a' * 32)
        self.assertTrue(self.reports[-1]['recovered']); self.assertEqual(len(self.s.moves), 2)
    def test_lost_promotion_response_is_reconciled(self):
        self.s.lost = True
        with self.assertRaises(RuntimeError): self.run_update()
        self.assertEqual(self.s.version, 'a' * 32); self.assertTrue(self.reports[-1]['recovered'])
    def test_external_version_change_never_overwritten_by_recovery(self):
        self.s.foreign = True
        with self.assertRaises(RuntimeError): self.run_update()
        self.assertEqual(self.s.version, 'b' * 32)
        self.assertFalse(self.s.moves); self.assertEqual(self.reports[-1]['phase'], 'conflict_manual_recovery')
    def test_secret_change_while_draining_blocks_promotion(self):
        original = self.k.drain
        def drift(op):
            original(op); self.s.values['c' * 32] = self.s.current()[1]; self.s.version = 'c' * 32
        self.k.drain = drift
        with self.assertRaises(RuntimeError): self.run_update()
        self.assertFalse(self.s.moves)


class PlanGuard(unittest.TestCase):
    def setUp(self):
        self.guard = module('runpod_plan', ROOT / 'scripts/check-v3-runpod-plan.py')
        fixture = json.loads((ROOT / 'tests/ci/v3/runpod-access-plan.fixture.json').read_text())
        self.plan, self.review = fixture['plan'], fixture['review']
    def test_native_terraform_bootstrap_fixture(self):
        self.assertEqual(self.guard.validate(self.plan, self.review), {'creates':4,'updates':0,'deletes':0,'newNodes':0})
    def test_no_deletion_update_or_compute(self):
        for action in [['update'], ['delete'], ['delete','create']]:
            value = copy.deepcopy(self.plan);value['resource_changes'][0]['change']['actions'] = action
            with self.assertRaises(ValueError): self.guard.validate(value, self.review)
        value = copy.deepcopy(self.plan);value['resource_changes'].append({'address':'aws_instance.gpu','change':{'actions':['create']}})
        with self.assertRaises(ValueError): self.guard.validate(value, self.review)
    def test_no_wildcard_or_other_secret_permissions(self):
        for mutation in ['action','resource']:
            value = copy.deepcopy(self.plan)
            policy = next(x['change']['after'] for x in value['resource_changes'] if 'role_policy' in x['address'])
            body = json.loads(policy['policy'])
            if mutation == 'action': body['Statement'][0]['Action'].append('secretsmanager:DeleteSecret')
            else: body['Statement'][0]['Resource'] = '*'
            policy['policy'] = json.dumps(body)
            with self.assertRaises(ValueError): self.guard.validate(value, self.review)
    def test_no_foreign_account_or_environment(self):
        value = copy.deepcopy(self.plan)
        provider = next(iter(value['configuration']['provider_config'].values()))
        provider['expressions']['allowed_account_ids']['constant_value'] = ['000000000000']
        with self.assertRaises(ValueError): self.guard.validate(value, self.review)
        value = copy.deepcopy(self.plan)
        role = next(x['change']['after'] for x in value['resource_changes'] if x['address'].startswith('aws_iam_role.'))
        role['assume_role_policy'] = role['assume_role_policy'].replace(':v3-production', ':other')
        with self.assertRaises(ValueError): self.guard.validate(value, self.review)


class Adapter(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.m = module('runtime_control', ROOT / 'deploy/v3/ai-apps/files/runtime/runpod_control.py')
        self.m.DIRECTORY = Path(self.temp.name)
        self.m.LEASE = self.m.DIRECTORY / 'lease.json'; self.m.STATE = self.m.DIRECTORY / 'state.json'
        self.tasks = []
    async def asyncTearDown(self):
        for task in self.tasks: task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.temp.cleanup()
    def lease(self, ttl=20):
        self.m.atomic(self.m.LEASE, {'operation': 'a' * 32, 'expires': time.time() + ttl})
    async def wait_for(self, fn):
        for _ in range(100):
            if fn(): return
            await asyncio.sleep(.01)
        self.fail('adapter deadline exceeded')
    async def test_drains_current_work_without_cancel_and_resumes(self):
        control = self.m.Control(1); started = asyncio.Event(); finish = asyncio.Event(); calls = []
        async def original(store, kind, stop):
            calls.append(kind); started.set()
            await finish.wait()  # represents either process, story or photo-batch work
            await stop.wait()
        t = asyncio.create_task(control.wrap(original)(None, 'process')); self.tasks.append(t)
        await started.wait(); self.tasks.append(control.monitor_task)
        self.lease(); await asyncio.sleep(.25)
        self.assertFalse(json.loads(self.m.STATE.read_text())['idle']); self.assertFalse(t.done())
        finish.set(); await self.wait_for(lambda: json.loads(self.m.STATE.read_text())['idle'])
        self.assertEqual(calls, ['process']); self.assertFalse(t.done())
        self.m.LEASE.unlink(); await self.wait_for(lambda: len(calls) == 2)
    async def test_lease_expiry_during_work_resumes_instead_of_crashing(self):
        control = self.m.Control(1); started = asyncio.Event(); finish = asyncio.Event(); calls = []
        async def original(store, kind, stop):
            calls.append(kind); started.set(); await finish.wait(); await stop.wait()
        t = asyncio.create_task(control.wrap(original)(None, 'story')); self.tasks.append(t)
        await started.wait(); self.tasks.append(control.monitor_task)
        self.lease(.3); await asyncio.sleep(.45); finish.set()
        await self.wait_for(lambda: len(calls) >= 2); self.assertFalse(t.done())
    async def test_http_gate_counts_existing_request_and_rejects_new(self):
        control = self.m.Control(0); started = asyncio.Event(); release = asyncio.Event()
        async def app(scope, receive, send): started.set(); await release.wait()
        async def send(_): pass
        t = asyncio.create_task(control.middleware(app)({'type': 'http', 'path': '/query'}, None, send)); self.tasks.append(t)
        await started.wait(); self.assertEqual(control.http, 1)
        self.lease(); messages = []
        async def collect(value): messages.append(value)
        await control.middleware(app)({'type': 'http', 'path': '/rewrite'}, None, collect)
        self.assertEqual(messages[0]['status'], 503); self.assertEqual(control.http, 1)
        release.set(); await t; self.assertEqual(control.http, 0)
    async def test_health_is_available_during_maintenance(self):
        self.lease(); control = self.m.Control(0); called = []
        async def app(*_): called.append(True)
        await control.middleware(app)({'type': 'http', 'path': '/health'}, None, None)
        self.assertEqual(called, [True])


class Transport(unittest.TestCase):
    def setUp(self): self.m = module('runpod_transport', ROOT / 'scripts/update-v3-runpod.py')
    def test_secret_body_uses_stdin_never_argv(self):
        from unittest.mock import patch
        result = type('Result', (), {'returncode':0,'stdout':'{}'})()
        with patch.object(self.m.subprocess, 'run', return_value=result) as run:
            self.m.aws('secretsmanager', 'put-secret-value', SecretString='fixture-private-key')
        args, kw = run.call_args
        self.assertNotIn('fixture-private-key', str(args))
        self.assertIn('fixture-private-key', kw['input'])
        self.assertIn('file:///dev/stdin', args[0])
    def test_force_refresh_annotation_restored(self):
        for initial in [None, 'previous-operator-value']:
            k = self.m.Kubernetes.__new__(self.m.Kubernetes)
            k.force_tokens = set(); k.original_annotation = None; k.annotation_captured = False
            row = {'metadata':{'resourceVersion':'1','annotations':{'keep':'value'}}}
            if initial is not None: row['metadata']['annotations']['force-sync'] = initial
            def api(method, path, data=None):
                if method == 'GET': return copy.deepcopy(row)
                value = data['metadata']['annotations']['force-sync']
                if value is None: row['metadata']['annotations'].pop('force-sync', None)
                else: row['metadata']['annotations']['force-sync'] = value
                return copy.deepcopy(row)
            k.api = api; k.pod = lambda _: {}; k.control = lambda *_: {'sourceMatches': True}
            k.until = lambda check, timeout: self.assertTrue(check())
            k.sync('fixture-hash')
            self.assertEqual(row['metadata']['annotations'].get('force-sync'), initial)
            self.assertEqual(row['metadata']['annotations']['keep'], 'value')
    def test_only_pending_states_are_retried(self):
        k = self.m.Kubernetes.__new__(self.m.Kubernetes)
        def unsafe(): raise ValueError('different release')
        with self.assertRaisesRegex(ValueError, 'different release'): k.until(unsafe, 60)


class Access(unittest.TestCase):
    def test_approved_workflow_serializes_with_cd(self):
        import yaml
        new = yaml.safe_load((ROOT / '.github/workflows/v3-runpod-update.yml').read_text())
        old = yaml.safe_load((ROOT / '.github/workflows/v3-approved-release.yml').read_text())
        self.assertEqual(new['concurrency'], old['concurrency'])
        self.assertEqual(new['jobs']['update']['environment'], 'v3-production')
        self.assertIn('V3_RUNPOD_UPDATE_ENABLED', json.dumps(new))
    def test_no_ecs_gpu_lifecycle_or_template_writes(self):
        s = (ROOT / 'scripts/update-v3-runpod.py').read_text()
        for forbidden in ['update-service', 'send-command', 'stop-instances', 'start-instances', 'rollout restart']:
            self.assertNotIn(forbidden, s)
        self.assertIn("'preconditions': {'uid': m['uid']", s)
    def test_rbac_has_no_secret_read_or_deployment_mutation(self):
        import yaml
        helm = os.environ.get('V3_HELM', 'helm')
        docs = list(yaml.safe_load_all(subprocess.check_output([helm, 'template', 'runpod', '--kube-version', '1.36.0', str(ROOT / 'deploy/v3/runpod-access'), '--set', 'enabled=true'], text=True)))
        self.assertEqual(len(docs), 4)
        for d in docs:
            self.assertIn(d['metadata']['namespace'], ['ai', 'argocd'])
            for rule in d.get('rules', []):
                self.assertNotIn('secrets', rule['resources']); self.assertNotIn('*', rule['verbs'])
                self.assertNotIn('*', rule['resources']); self.assertNotIn('*', rule['apiGroups'])
                if 'deployments' in rule['resources']: self.assertEqual(rule['verbs'], ['get'])
        disabled = subprocess.check_output([helm, 'template', 'runpod', '--kube-version', '1.36.0', str(ROOT / 'deploy/v3/runpod-access')], text=True)
        self.assertFalse(disabled.strip())


if __name__ == '__main__': unittest.main()

class ModelProbeRegression(unittest.TestCase):
    """A local gateway rejects urllib's default agent and detects credential redirects."""
    def setUp(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import urllib.request
        self.mode = 'good'; self.leaks = 0; self.requests = []
        case = self
        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                if self.path == '/leak': case.leaks += 1
                case.requests.append((self.path, self.headers.get('User-Agent'), self.headers.get('Authorization')))
                if case.mode == 'redirect':
                    self.send_response(302); self.send_header('Location', '/leak'); self.end_headers(); return
                if self.headers.get('User-Agent') != 'python-httpx/0.28.1' or case.mode == 'denied':
                    self.send_response(403); self.end_headers(); self.wfile.write(b'error code: 1010'); return
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({'data':[{'id':'other' if case.mode == 'wrong-model' else 'gemma-4'}]}).encode())
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.original_builder = urllib.request.build_opener
        self.transport = module('probe_transport', ROOT / 'scripts/update-v3-runpod.py')
        self.adapter = module('probe_adapter', ROOT / 'deploy/v3/ai-apps/files/runtime/runpod_control.py')
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
    def build_gateway(self, *handlers):
        import urllib.request
        opener = self.original_builder(*handlers)
        case = self
        class LocalGateway:
            def open(self, request, timeout):
                case.assertEqual(request.full_url, 'https://fixture-8000.proxy.runpod.net/v1/models')
                case.assertGreater(timeout, 0); case.assertLessEqual(timeout, 25)
                routed = urllib.request.Request('http://127.0.0.1:' + str(case.server.server_port) + '/v1/models', headers=dict(request.header_items()))
                return opener.open(routed, timeout=timeout)
        return LocalGateway()
    def invoke(self, target):
        import contextlib, hashlib, io, urllib.request
        from unittest.mock import patch
        url = 'https://fixture-8000.proxy.runpod.net/v1'
        with patch.object(urllib.request, 'build_opener', side_effect=self.build_gateway):
            if target == 'transport': return self.transport.probe(url, 'fixture-key', 'gemma-4')
            with patch.dict(os.environ, {'VLM_BASE_URL':url,'VLM_API_KEY':'fixture-key','VLM_MODEL':'gemma-4'}), patch.object(sys,'argv',['control','verify',hashlib.sha256(url.encode()).hexdigest()]), contextlib.redirect_stdout(io.StringIO()) as output:
                self.adapter.cli()
            return json.loads(output.getvalue())
    def test_both_probes_pass_gateway_that_rejects_default_urllib(self):
        import urllib.request, urllib.error
        with self.assertRaises(urllib.error.HTTPError):
            self.original_builder().open('http://127.0.0.1:' + str(self.server.server_port) + '/v1/models')
        for target in ['transport', 'adapter']:
            with self.subTest(target=target):
                result=self.invoke(target)
                if target=='adapter': self.assertEqual(result, {'environmentMatches':True,'modelReady':True})
        self.assertEqual(self.requests[-2:], [('/v1/models','python-httpx/0.28.1','Bearer fixture-key')]*2)
    def test_real_denials_remain_failures(self):
        import urllib.error
        self.mode = 'denied'
        for target in ['transport', 'adapter']:
            with self.subTest(target=target), self.assertRaises(urllib.error.HTTPError): self.invoke(target)
    def test_redirect_never_forwards_credentials(self):
        import urllib.error
        self.mode = 'redirect'
        for target in ['transport', 'adapter']:
            with self.subTest(target=target), self.assertRaises(urllib.error.HTTPError): self.invoke(target)
        self.assertEqual(self.leaks, 0)
    def test_unexpected_model_is_not_accepted(self):
        self.mode = 'wrong-model'
        with self.assertRaises(ValueError): self.invoke('transport')
        # Adapter reports false rather than claiming the replacement model is healthy.
        self.assertEqual(self.invoke('adapter'), {'environmentMatches':True,'modelReady':False})
