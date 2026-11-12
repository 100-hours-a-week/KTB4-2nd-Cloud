#!/usr/bin/env python3
"""Actual pinned MongoDB TLS fixture; no AWS, host ports or production data."""
import importlib.util
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / 'deploy/v3/mongodb'
IMAGE = next(line.split(': ', 1)[1] for line in (CHART / 'values.yaml').read_text().splitlines() if line.startswith('image: '))


def docker(*args, check=True, timeout=45):
    p = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError('Isolated Docker operation failed: ' + args[0] + '; details suppressed')
    return p


def exercise():
    name = 'v3-mongo-health-' + secrets.token_hex(5)
    spec = importlib.util.spec_from_file_location('synthetic_mongo_secret', ROOT / 'scripts/create-v3-mongodb-bootstrap-secret.py')
    bootstrap = importlib.util.module_from_spec(spec); spec.loader.exec_module(bootstrap)
    # Public immutable image only. Do not silently fall back to a mutable tag.
    if docker('image', 'inspect', IMAGE, check=False).returncode:
        docker('pull', IMAGE, timeout=180)
    with tempfile.TemporaryDirectory(prefix=name) as private:
        root = Path(private); root.chmod(0o755)
        certs = root / 'certs'; certs.mkdir()
        payload = bootstrap.create_payload(certs)
        creds = root / 'credentials'; creds.mkdir(mode=0o755)
        for field, filename in [('adminPassword', 'admin-password'), ('appPassword', 'app-password'),
                                ('healthPassword', 'health-password'), ('tlsCa', 'ca.crt'),
                                ('tlsCert', 'tls.crt'), ('tlsKey', 'tls.key')]:
            p = creds / filename; p.write_text(payload[field]); p.chmod(0o444)
        data = root / 'data'; data.mkdir(mode=0o777); data.chmod(0o777)
        runtime = root / 'runtime'; shutil.copytree(CHART / 'files', runtime)
        for p in runtime.iterdir(): p.chmod(0o444)
        def execute(*args, check=True, timeout=45):
            return docker('exec', name, *args, check=check, timeout=timeout)
        def probe(check=False):
            return execute('/bin/bash', '/runtime/health-probe.sh', check=check)
        def wait_health():
            end = time.monotonic() + 180
            while time.monotonic() < end:
                if probe().returncode == 0: return
                if docker('inspect', '-f', '{{.State.Running}}', name).stdout.strip() != 'true':
                    raise RuntimeError('Synthetic MongoDB exited')
                time.sleep(2)
            raise RuntimeError('Synthetic authenticated health deadline exceeded')
        def shell(js, check=True):
            script = root / 'runtime' / 'trial.js'
            if script.exists(): script.chmod(0o644)
            script.write_text("load('/runtime/common.js');\n" + js); script.chmod(0o444)
            return execute('/bin/bash', '/runtime/shell.sh', '/runtime/trial.js', check=check)
        try:
            docker('run', '-d', '--name', name, '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                   '--security-opt', 'no-new-privileges', '--user', '999:999', '--cpus', '0.5', '--memory', '1g',
                   '--env', 'NODE_OPTIONS=--max-old-space-size=128',
                   '--tmpfs', '/tmp:uid=999,gid=999,mode=1777,size=16m',
                   '-v', str(runtime) + ':/runtime:ro', '-v', str(runtime / 'mongosh.conf') + ':/etc/mongosh.conf:ro',
                   '-v', str(creds) + ':/credentials:ro', '-v', str(data) + ':/data/db',
                   '--entrypoint', '/bin/bash', IMAGE, '/runtime/entrypoint.sh')
            wait_health()
            print(json.dumps({'stage': 'authenticated_ready'}), flush=True)
            # Same CPU/memory cap, same real authentication. Times are local, not production SLOs.
            start = time.monotonic()
            execute('/bin/bash', '/runtime/shell.sh', '/runtime/health.js')
            cold_ms = round((time.monotonic() - start) * 1000)
            monitor = execute('cat', '/tmp/mongodb-health/monitor.pid').stdout.strip()
            start = time.monotonic()
            for _ in range(5): probe(check=True)
            cached_ms = round((time.monotonic() - start) * 1000 / 5)
            assert execute('cat', '/tmp/mongodb-health/monitor.pid').stdout.strip() == monitor
            print(json.dumps({'stage': 'timings', 'oneShotMs': cold_ms, 'cachedMeanMs': cached_ms}), flush=True)
            shell("safely(() => {const d = new Mongo(uri('127.0.0.1','app','yeodam','app')).getDB('yeodam'); d.getCollection('_health_fixture').insertOne({_id: 661, marker: 'synthetic'});});")
            # A healthy TCP port or stale PASS alone cannot satisfy authentication.
            wrong = creds / 'health-password'; original = wrong.read_text(); wrong.chmod(0o644); wrong.write_text('0' * 48); wrong.chmod(0o444)
            assert execute('/bin/bash', '/runtime/shell.sh', '/runtime/health.js', check=False).returncode != 0
            wrong.chmod(0o644); wrong.write_text(original); wrong.chmod(0o444)
            # The cached success must turn into failure when the database stops responding.
            docker('kill', '--signal', 'STOP', name)
            end = time.monotonic() + 25
            while probe().returncode == 0 and time.monotonic() < end: time.sleep(1)
            assert probe().returncode != 0
            docker('kill', '--signal', 'CONT', name); wait_health()
            # Freeze only the isolated monitor, then exercise false-positive boundaries.
            execute('/bin/bash', '-c', 'kill -STOP "$(cat /tmp/mongodb-health/monitor.pid)"')
            for change in [
                'read -r t p h < /tmp/mongodb-health/status; printf "%s %s 1\\n" "$(( $(date +%s) - 31 ))" "$p" > /tmp/mongodb-health/status',
                'read -r t p h < /tmp/mongodb-health/status; printf "%s %s 1\\n" "$(( $(date +%s) + 100 ))" "$p" > /tmp/mongodb-health/status',
                'printf "%s 99999999 1\\n" "$(date +%s)" > /tmp/mongodb-health/status',
            ]:
                execute('/bin/bash', '-c', change); assert probe().returncode != 0
            execute('/bin/bash', '-c', 'kill -CONT "$(cat /tmp/mongodb-health/monitor.pid)"'); wait_health()
            execute('/bin/bash', '-c', 'kill -TERM "$(cat /tmp/mongodb-health/monitor.pid)"')
            time.sleep(1); assert probe().returncode != 0
            # Container restart: retained DB must survive, old process evidence must not.
            print(json.dumps({'stage': 'failure_boundaries_passed'}), flush=True)
            docker('restart', '-t', '30', name); wait_health()
            shell("safely(() => {const d = new Mongo(uri('127.0.0.1','app','yeodam','app')).getDB('yeodam'); if (d.getCollection('_health_fixture').findOne({_id:661}).marker !== 'synthetic') throw new Error('Data lost');});")
            assert docker('inspect', '-f', '{{.State.OOMKilled}}', name).stdout.strip() == 'false'
            return {'tlsAuthenticated': True, 'cachedProbeMeanMs': cached_ms, 'oneShotProbeMs': cold_ms,
                    'sameMonitorForRepeatedProbes': True, 'wrongCredentialRejected': True,
                    'unresponsiveDatabaseRejected': True,
                    'staleFutureAndWrongPidRejected': True, 'deadMonitorRejected': True,
                    'retainedDataAfterContainerRestart': True, 'cpus': 0.5, 'memoryLimit': '1Gi', 'productionAccess': False}
        finally:
            docker('rm', '-f', name, check=False)


if __name__ == '__main__':
    print(json.dumps(exercise()))
