from pathlib import Path
import importlib.util
import json
from subprocess import CompletedProcess, TimeoutExpired
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import ci_image_transport as images
spec = importlib.util.spec_from_file_location('nginx_build', ROOT / 'scripts/ci-build-nginx-fixture.py')
nginx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nginx)
DIGEST = 'sha256:' + 'a' * 64
SOURCE = images.PRIMARY + '@' + DIGEST
MIRROR = images.MIRROR + '@' + DIGEST
ENV = {'GITHUB_ACTIONS': 'true', 'RUNNER_OS': 'Linux', 'RUNNER_ENVIRONMENT': 'github-hosted'}


def result(code=0, out='', error=''):
    return CompletedProcess([], code, out, error)


class Transport(unittest.TestCase):
    def test_throttling_uses_only_same_digest_mirror_after_bounded_retry(self):
        calls = []; sleeps = []; ready = set()
        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[1:3] == ['image', 'inspect']:
                ref = cmd[-1]
                if ref not in ready: return result(1)
                return result(out=json.dumps([{'Os':'linux','Architecture':'amd64','RepoDigests':['redis@'+DIGEST]}]))
            if cmd[-1] == SOURCE: return result(1, error='toomanyrequests: Rate exceeded')
            self.assertEqual(cmd[-1], MIRROR); ready.add(MIRROR); return result()
        self.assertEqual(images.pull(SOURCE, run, sleeps.append), MIRROR)
        self.assertEqual(sum(c[1] == 'pull' for c in calls), 4)
        self.assertEqual(sleeps, [2, 5])

    def test_auth_and_unknown_image_do_not_retry_or_fallback(self):
        for error in ['unauthorized: authentication required','manifest unknown','unexpected failure']:
            pulls = []
            def run(cmd, **kw):
                if cmd[1] == 'pull': pulls.append(cmd); return result(1, error=error)
                return result(1)
            with self.assertRaises(RuntimeError): images.pull(SOURCE, run, lambda _: self.fail('unexpected sleep'))
            self.assertEqual(len(pulls), 1)

    def test_pull_timeouts_are_bounded_transient_failures(self):
        pulls=[]
        def run(cmd, **kw):
            if cmd[1] == 'pull':
                pulls.append(cmd)
                raise TimeoutExpired(cmd,kw['timeout'])
            return result(1)
        with self.assertRaises(RuntimeError): images.pull(SOURCE,run,lambda _:None)
        self.assertEqual(len(pulls),6)

    def test_wrong_platform_and_digest_are_rejected(self):
        for row in [{'Os':'linux','Architecture':'arm64','RepoDigests':[SOURCE]},
                    {'Os':'linux','Architecture':'amd64','RepoDigests':[images.PRIMARY+'@sha256:'+'b'*64]}]:
            with self.assertRaises(ValueError): images.inspect(SOURCE, lambda *a, **k: result(out=json.dumps([row])))
        for ref in ['redis:latest','other.example/redis@'+DIGEST]:
            with self.assertRaises(ValueError): images.pull(ref, lambda *a, **k: self.fail('unreviewed registry called'))

    def test_all_sources_exhaust_at_six_pulls(self):
        pulls=[]
        def run(cmd, **kw):
            if cmd[1] == 'pull': pulls.append(cmd)
            return result(1,error='toomanyrequests: Rate exceeded')
        with self.assertRaises(RuntimeError):images.pull(SOURCE,run,lambda _: None)
        self.assertEqual(len(pulls),6)

    def test_healthy_build_never_uses_host_network(self):
        calls=[]
        def run(cmd, **kw): calls.append((cmd,kw)); return result()
        self.assertFalse(nginx.build(run, lambda: self.fail('DNS check not needed'), {}, 'Darwin')['recovered'])
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][0][-1],'-')
        self.assertNotIn('--network=host',calls[0][0])
        self.assertNotIn('COPY',calls[0][1]['input'])

    def test_dns_recovery_only_after_healthy_host_and_probe(self):
        calls=[]; replies=iter([result(1,error="Could not resolve 'archive.ubuntu.com'"),result(),result()])
        def run(cmd, **kw): calls.append((cmd,kw)); return next(replies)
        proof=nginx.build(run,lambda:dict.fromkeys(nginx.DOMAINS,True),ENV,'Linux')
        self.assertTrue(proof['recovered']);self.assertEqual(len(calls),3)
        self.assertIn('--network=host',calls[1][0]);self.assertIn('getent hosts',calls[1][1]['input'])
        self.assertIn('--network=host',calls[2][0])
        self.assertEqual(proof['runtimeNetwork'],'none')

    def test_host_dns_failure_local_and_non_dns_fail_closed(self):
        for env,system,healthy,error in [({},'Linux',True,'Could not resolve'),(ENV,'Darwin',True,'Could not resolve'),(ENV,'Linux',True,'package version unavailable')]:
            calls=[]
            def run(cmd, **kw):calls.append(cmd);return result(1,error=error)
            with self.assertRaises(RuntimeError):nginx.build(run,lambda:dict.fromkeys(nginx.DOMAINS,healthy),env,system)
            self.assertEqual(len(calls),1)

    def test_host_dns_failure_uses_only_public_fixed_build_bindings(self):
        calls=[];replies=iter([result(1,error='Could not resolve'),result(),result()])
        def run(cmd, **kw):calls.append(cmd);return next(replies)
        addresses={'archive.ubuntu.com':['91.189.91.81'],'security.ubuntu.com':['185.125.190.81']}
        proof=nginx.build(run,lambda:dict.fromkeys(nginx.DOMAINS,False),ENV,'Linux',lambda:addresses)
        self.assertEqual(proof['buildNetwork'],'https-dns-build-hosts')
        self.assertIn('archive.ubuntu.com=91.189.91.81',calls[2])
        self.assertNotIn('--network=host',calls[2])
        self.assertEqual(len(calls),3)

    def test_https_dns_rejects_wrong_question_private_addresses_and_empty_answers(self):
        good={'Status':0,'Question':[{'name':'archive.ubuntu.com.','type':1}], 'Answer':[{'name':'archive.ubuntu.com.','type':1,'TTL':60,'data':'91.189.91.81'}]}
        import copy
        self.assertEqual(nginx.parse_addresses('archive.ubuntu.com',good),['91.189.91.81'])
        variants=[]
        for ip in ['127.0.0.1','10.0.0.1','169.254.169.254','::1']:
            bad=copy.deepcopy(good);bad['Answer'][0]['data']=ip;variants.append(bad)
        for field,value in [('Status',3),('TC',True),('Question',[{'name':'other.example.','type':1}]),('Answer',[])]:
            bad=copy.deepcopy(good);bad[field]=value;variants.append(bad)
        bad=copy.deepcopy(good);bad['Answer'][0]['TTL']=0;variants.append(bad)
        for bad in variants:
            with self.assertRaises(ValueError):nginx.parse_addresses('archive.ubuntu.com',bad)

    def test_host_build_probe_failure_blocks_package_retry(self):
        calls=[]
        def run(cmd, **kw):calls.append(cmd);return result(1,error='Could not resolve')
        with self.assertRaises(RuntimeError):nginx.build(run,lambda:dict.fromkeys(nginx.DOMAINS,True),ENV,'Linux')
        self.assertEqual(len(calls),2)

    def test_runtime_network_isolation_remains_and_workflow_uses_verified_ref(self):
        shell=(ROOT/'tests/ci/check-nginx-jwt-gate.sh').read_text()
        self.assertEqual(shell.count('--network none'),2)
        self.assertIn('python3 scripts/ci-build-nginx-fixture.py',shell)
        workflow=(ROOT/'.github/workflows/v3-platform-check.yml').read_text()
        self.assertIn('python3 scripts/verify-v3-redis-aof.py --image "$selected"',workflow)
        self.assertIn('python3 scripts/verify-v3-redis-aof.py --store ai --image "$ai_selected"',workflow)

if __name__ == '__main__': unittest.main()
