#!/usr/bin/env python3
"""Environment-approved URL rotation through fixed EKS TLS tunnel and ESO."""
import argparse
import base64
import contextlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.request
from v3_runpod_update import SECRET, WORKLOADS, MODEL, require, sha, perform

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = '483175530259'
REGION = 'ap-northeast-2'
CLUSTER = 'yeodam-v3-foundation'
ROLE = 'yeodam-v3-foundation-runpod-update'
DOCUMENT = 'Yeodam-V3-RunPod-Update'
PORT = 18574


def aws(service, action, **payload):
    # SecretString travels over stdin only, never argv, disk, stdout or exceptions.
    p = subprocess.run(['aws', service, action, '--region', REGION, '--output', 'json',
                        '--cli-input-json', 'file:///dev/stdin'],
                       input=json.dumps(payload), text=True, capture_output=True, timeout=45)
    require(p.returncode == 0, 'AWS operation failed; sensitive response suppressed')
    return json.loads(p.stdout or '{}')


class Secrets:
    def current(self):
        row = aws('secretsmanager', 'get-secret-value', SecretId=SECRET, VersionStage='AWSCURRENT')
        return row['VersionId'], json.loads(row['SecretString'])

    def stage(self, operation, value):
        aws('secretsmanager', 'put-secret-value', SecretId=SECRET, ClientRequestToken=operation,
            SecretString=json.dumps(value), VersionStages=['v3-runpod-' + operation])

    def move(self, new, previous):
        aws('secretsmanager', 'update-secret-version-stage', SecretId=SECRET, VersionStage='AWSCURRENT',
            MoveToVersionId=new, RemoveFromVersionId=previous)


def probe(url, key, model):
    require(isinstance(key, str) and key and '\n' not in key and '\r' not in key, 'Invalid V3 VLM credential')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    request = urllib.request.Request(url + '/models', headers={'Authorization': 'Bearer ' + key})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=25) as response:
        require(response.status == 200, 'New model endpoint unavailable')
        body = json.loads(response.read(1024 * 1024))
    require(any(x.get('id') == model for x in body.get('data', [])), 'Expected generation model unavailable')


class Connection(http.client.HTTPSConnection):
    def connect(self):
        raw = socket.create_connection(('127.0.0.1', PORT), timeout=self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class Pending(RuntimeError):
    """Only normal reconciliation waits may be retried."""


class Kubernetes:
    def __init__(self, host, ca, kubeconfig, wait=1800):
        self.host, self.context, self.kubeconfig = host, ssl.create_default_context(cadata=ca), kubeconfig
        self.wait = wait
        self.known = {}
        self.baseline = {}
        self.revision = None
        self.force_tokens = set()
        self.original_annotation = None
        self.annotation_captured = False

    def api(self, method, path, value=None):
        # get-token uses CLI spelling rather than the EKS API request schema.
        p = subprocess.run(['aws', 'eks', 'get-token', '--cluster-name', CLUSTER, '--region', REGION],
                           capture_output=True, text=True, timeout=30)
        require(p.returncode == 0, 'EKS authentication unavailable')
        token = json.loads(p.stdout)['status']['token']
        c = Connection(self.host, timeout=20, context=self.context)
        try:
            headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' if method != 'PATCH' else 'application/merge-patch+json'}
            c.request(method, path, json.dumps(value) if value is not None else None, headers)
            response = c.getresponse()
            body = response.read(8 * 1024 * 1024)
            require(response.status in {200, 201, 202}, 'Kubernetes request rejected; response suppressed')
            return json.loads(body)
        finally:
            c.close()

    def get(self, resource, name):
        prefix = '/apis/apps/v1' if resource in {'deployments', 'replicasets'} else '/api/v1'
        return self.api('GET', prefix + '/namespaces/ai/' + resource + '/' + name)

    def application(self):
        a = self.api('GET', '/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/yeodam-v3-apps')
        require(a['spec']['source']['targetRevision'] == 'deploy/v3-approved', 'Unexpected app source')
        if a['status']['sync']['status'] != 'Synced' or a['status']['health']['status'] != 'Healthy':
            raise Pending('Approved app CD is reconciling')
        return a['status']['sync']['revision']

    def pod(self, name, ready=True):
        d = self.get('deployments', name)
        require(d['spec']['replicas'] == 1, 'Preserve exactly one minimum replica')
        if name in self.baseline:
            require(d['spec'] == self.baseline[name], 'Deployment changed; do not mix with another release')
        selector = 'app.kubernetes.io%2Finstance%3D' + name + '%2Cyeodam.io%2Fstage%3Dv3'
        rows = self.api('GET', '/api/v1/namespaces/ai/pods?labelSelector=' + selector)['items']
        rows = [p for p in rows if not p['metadata'].get('deletionTimestamp')]
        if not rows: raise Pending('Replacement Pod not created yet')
        require(len(rows) == 1, 'Exactly one current AI Pod required')
        p = rows[0]
        owner = [x for x in p['metadata']['ownerReferences'] if x.get('controller')]
        require(len(owner) == 1 and owner[0]['kind'] == 'ReplicaSet', 'AI Pod must belong to its Deployment')
        rs = self.get('replicasets', owner[0]['name'])
        require(rs['metadata']['uid'] == owner[0]['uid'] and any(x.get('controller') and x['uid'] == d['metadata']['uid'] for x in rs['metadata']['ownerReferences']), 'Wrong Pod owner')
        require([c['name'] for c in p['spec']['containers']] == ['ai'], 'Only the existing AI container')
        require(p['spec']['containers'][0]['image'] == d['spec']['template']['spec']['containers'][0]['image'], 'Image changed')
        if ready:
            if not any(x['type'] == 'Ready' and x['status'] == 'True' for x in p.get('status', {}).get('conditions', [])):
                raise Pending('AI Pod not Ready')
        self.known[p['metadata']['uid']] = p['metadata']['name']
        return p

    def control(self, pod, action, *args):
        command = ['kubectl', '--kubeconfig', str(self.kubeconfig), '-n', 'ai', 'exec', pod['metadata']['name'], '-c', 'ai', '--',
                   'python', '/runtime/runpod_control.py', action, *args]
        p = subprocess.run(command, capture_output=True, text=True, timeout=35)
        require(p.returncode == 0 and len(p.stdout) < 4096, 'AI runtime adapter unavailable; response suppressed')
        return json.loads(p.stdout)

    def preflight(self, expected):
        self.revision = self.application()
        for name in WORKLOADS:
            d = self.get('deployments', name)
            require(d['status'].get('observedGeneration') == d['metadata']['generation'] and d['status'].get('readyReplicas') == 1, 'AI rollout incomplete')
            self.baseline[name] = d['spec']
            p = self.pod(name)
            s = self.control(p, 'status')
            require(s.get('protocol') == 1 and not s['draining'], 'Bootstrap the reviewed Cloud adapter first; no existing maintenance')
            require(self.control(p, 'source', expected)['sourceMatches'], 'ESO source differs from AWSCURRENT')
            env = self.control(p, 'environment', expected)
            require(env['environmentMatches'] and env['modelMatches'], 'Current Pod settings differ from V3 source')

    def until(self, check, timeout):
        end = time.monotonic() + timeout
        while True:
            try:
                if check(): return
            except Pending:
                pass
            require(time.monotonic() < end, 'Bounded AI transition timed out')
            time.sleep(3)

    def drain(self, operation):
        for name in WORKLOADS:
            self.control(self.pod(name), 'drain', operation, '3600')
        self.until(lambda: self.idle(operation), self.wait)

    def idle(self, operation):
        for name in WORKLOADS:
            s = self.control(self.pod(name), 'status')
            if s.get('operation') != operation or not s.get('idle'): return False
        return True

    def assert_idle(self, operation):
        require(self.idle(operation), 'AI work is not safely drained')

    def restore_annotation(self):
        if not self.annotation_captured: return
        path = '/apis/external-secrets.io/v1/namespaces/ai/externalsecrets/ai-runtime'
        row = self.api('GET', path)
        if row['metadata'].get('annotations', {}).get('force-sync') in self.force_tokens:
            self.api('PATCH', path, {'metadata': {'resourceVersion': row['metadata']['resourceVersion'],
                      'annotations': {'force-sync': self.original_annotation}}})

    def sync(self, expected):
        path = '/apis/external-secrets.io/v1/namespaces/ai/externalsecrets/ai-runtime'
        row = self.api('GET', path)
        if not self.annotation_captured:
            self.original_annotation = row['metadata'].get('annotations', {}).get('force-sync')
            self.annotation_captured = True
        nonce = str(time.time_ns())
        self.force_tokens.add(nonce)
        try:
            self.api('PATCH', path, {'metadata': {'resourceVersion': row['metadata']['resourceVersion'],
                     'annotations': {'force-sync': nonce}}})
            self.until(lambda: all(self.control(self.pod(n), 'source', expected)['sourceMatches'] for n in WORKLOADS), 240)
        finally:
            self.restore_annotation()

    def replace(self, name, operation, expected):
        p = self.pod(name)
        self.control(p, 'drain', operation, '3600')
        state = self.control(p, 'status')
        require(state.get('operation') == operation and state.get('idle') and state.get('leaseRemainingSeconds', 0) >= 60, 'Only an owned, drained Pod may be replaced')
        m = p['metadata']
        self.api('DELETE', '/api/v1/namespaces/ai/pods/' + m['name'],
                 {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': m['uid'], 'resourceVersion': m['resourceVersion']}})
        def replacement():
            new = self.pod(name)
            if new['metadata']['uid'] == m['uid']: return False
            return self.control(new, 'verify', expected).get('modelReady') is True
        self.until(replacement, 900)

    def complete(self):
        def unchanged():
            require(self.application() == self.revision, 'Approved revision changed during URL update')
            return True
        self.until(unchanged, 180)

    def resume(self, operation):
        try: self.restore_annotation()
        except Exception: pass
        for name in WORKLOADS:
            try:
                self.control(self.pod(name), 'resume', operation)
            except Exception:
                # An unknown/replaced/dead Pod cannot be safely commanded. The
                # original lease expires without a global Redis write/reset.
                pass


@contextlib.contextmanager
def tunnel(instance, directory):
    identity = aws('sts', 'get-caller-identity')
    require(identity['Account'] == ACCOUNT and re.fullmatch(r'arn:aws:sts::483175530259:assumed-role/' + ROLE + r'/v3-runpod-[0-9]+', identity['Arn']), 'Use only the approved V3 RunPod role')
    cluster = aws('eks', 'describe-cluster', name=CLUSTER)['cluster']
    require(cluster['status'] == 'ACTIVE' and cluster['resourcesVpcConfig']['vpcId'] == 'vpc-0ecc3132f3b2170e9', 'Wrong V3 cluster')
    require(re.fullmatch('i-[a-f0-9]{17}', instance), 'Exact existing system instance required')
    host = cluster['endpoint'].removeprefix('https://')
    ca = base64.b64decode(cluster['certificateAuthority']['data']).decode()
    config = directory / 'kubeconfig.json'
    config.write_text(json.dumps({'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'v3-runpod',
      'clusters': [{'name': 'v3', 'cluster': {'server': 'https://127.0.0.1:' + str(PORT), 'tls-server-name': host,
       'certificate-authority-data': cluster['certificateAuthority']['data']}}],
      'users': [{'name': 'v3', 'user': {'exec': {'apiVersion': 'client.authentication.k8s.io/v1beta1', 'command': 'aws',
       'args': ['eks', 'get-token', '--cluster-name', CLUSTER, '--region', REGION], 'interactiveMode': 'Never'}}}],
      'contexts': [{'name': 'v3-runpod', 'context': {'cluster': 'v3', 'user': 'v3'}}]}))
    config.chmod(0o600)
    sessions = []
    process = subprocess.Popen(['aws', 'ssm', 'start-session', '--region', REGION, '--target', instance, '--document-name', DOCUMENT],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    def consume():
        for line in process.stdout:
            match = re.search(r'Starting session with SessionId: ([A-Za-z0-9_:-]+)', line)
            if match: sessions.append(match[1])
    thread = threading.Thread(target=consume, daemon=True); thread.start()
    try:
        start = time.monotonic()
        while True:
            require(process.poll() is None, 'Management tunnel exited')
            try:
                with socket.create_connection(('127.0.0.1', PORT), timeout=.2): break
            except OSError:
                require(time.monotonic() - start < 45, 'Management tunnel unavailable'); time.sleep(.2)
        yield Kubernetes(host, ca, config)
    finally:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        thread.join(timeout=2)
        if len(sessions) == 1:
            aws('ssm', 'terminate-session', SessionId=sessions[0])
        config.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', required=True); p.add_argument('--instance', required=True); p.add_argument('--report', required=True, type=Path)
    a = p.parse_args(); os.umask(0o077)
    def save(value):
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(json.dumps(value, indent=2) + '\n')
    def interrupt(*_): raise InterruptedError('Workflow interrupted')
    signal.signal(signal.SIGTERM, interrupt)
    save({"completed": False, "phase": "access_preflight"})
    try:
        with tempfile.TemporaryDirectory(prefix='v3-runpod-') as d, tunnel(a.instance, Path(d)) as k:
            result = perform(a.url, Secrets(), k, probe, save)
        print(json.dumps({'completed': result['completed'], 'phase': result['phase']}))
    except BaseException as e:
        if a.report.exists():
            report = json.loads(a.report.read_text())
            report.update(workflowFailed=True, errorType=type(e).__name__)
            save(report)
        print('V3 URL update failed; inspect the public report. Sensitive diagnostics suppressed.')
        raise SystemExit(1) from None


if __name__ == '__main__': main()
