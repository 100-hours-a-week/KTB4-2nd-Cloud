"""Explicit post-handoff file installation / V1-only passthrough lifecycle.

No AWS creation, Nginx replacement/reload, DB/Redis write or authentication cutover.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import subprocess
import time
from urllib.request import build_opener, ProxyHandler

from .staging import gate_config, read_json, signature, SOURCE, validate

BASE = Path('/opt/yeodam/request-cutover/runs')
STATE = Path('/var/lib/yeodam/request-cutover')


def verify_bundle(bundle):
    if bundle.is_symlink() or bundle.stat().st_mode & 0o077:
        raise ValueError('bundle must be a private directory')
    record = read_json(bundle / 'bundle.json')
    for name, digest in record['files'].items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('invalid bundle path')
        path = bundle / name
        ancestry = [bundle.joinpath(*relative.parts[:end]) for end in range(1, len(relative.parts) + 1)]
        if any(parent.is_symlink() for parent in ancestry) or not path.is_file():
            raise ValueError('bundle must contain only regular files')
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('bundle content changed; render again')
    plan = validate(read_json(bundle / 'plan.json'))
    if record['run_id'] != plan['run_id'] or record['plan_hash'] != signature(plan):
        raise ValueError('bundle plan identity mismatch')
    if read_json(bundle / 'gate.json') != gate_config(plan):
        raise ValueError('runtime does not match reviewed plan')
    return plan, record


def require_ready(plan, report, now=None):
    now = now or datetime.now(timezone.utc)
    age = (now - datetime.fromisoformat(report['observed_at'])).total_seconds()
    if (not plan['handoff']['db_tests_complete'] or not plan['mysql']['expected_uuid']
            or not plan['handoff']['evidence'] or report.get('mode') != 'host-read-only'
            or report.get('result') != 'pass' or not 0 <= age <= 300
            or report.get('run_id') != plan['run_id'] or report.get('plan_hash') != signature(plan)):
        raise ValueError('fresh matching host preflight and DB-test handoff required')
    required = {'fresh_matching_inventory','source_instance','alb_upstream_tls_name',
        'backend_targets_healthy','v2_uses_migration_database','v2_public_contract',
        'v2_redis_contract','db_test_handoff','executed_on_source_host','v1_health',
        'alb_health_verified_tls','both_routes_current_writer','expanded_schema_and_drained',
        'redis_tls_and_inactive_namespace'}
    if set(report.get('checks', {})) != required or any(v is not True for v in report['checks'].values()):
        raise ValueError('incomplete or failed host preflight')


@contextmanager
def operation_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another staging operation is running') from None
        yield
    finally:
        os.close(fd)


def run(*args):
    return subprocess.check_output(list(args), stderr=subprocess.PIPE, text=True).strip()


def compose(directory, *args):
    try:
        run('docker', 'compose', 'version')
        prefix = ['docker', 'compose']
    except subprocess.CalledProcessError:
        prefix = ['docker-compose']
    run_id = read_json(directory / 'bundle.json')['run_id']
    return run(*prefix, '--project-name', 'yeodam-cutover-stage-' + run_id[:8],
        '--env-file', str(directory / 'runtime.env'), '-f', str(directory / 'compose.yml'), *args)


def status(directory, state):
    class UnixHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.settimeout(2)
            self.sock.connect(str(state / 'control.sock'))
    with UnixHTTP('localhost', timeout=2) as client:
        client.request('GET', '/status')
        response = client.getresponse()
        if response.status != 200:
            raise ValueError('gate status unavailable')
        return json.loads(response.read())


def require_detached(nginx_dump, gate_state):
    # Conservative: do not stop while any loaded Nginx config refers to this port.
    inflight = gate_state.get('inflight', {})
    if ('19091' in nginx_dump or set(inflight) != {'V1', 'V2'} or gate_state.get('phase') != 'V1'
            or gate_state.get('waiters') != 0
            or any(gate_state.get('inflight', {}).values())):
        raise ValueError('detach Nginx, finish requests and retain V1 authority before stopping')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bundle', required=True, type=Path)
    p.add_argument('command', choices=('plan', 'install', 'start', 'status', 'stop'))
    p.add_argument('--confirm-run')
    p.add_argument('--report', type=Path)
    args = p.parse_args()
    try:
        plan, record = verify_bundle(args.bundle)
        directory, state = BASE / plan['run_id'], STATE / plan['run_id']
        if args.command == 'plan':
            print(json.dumps({'run_id': plan['run_id'], 'destination': str(directory),
                'state_directory': str(state), 'new_servers': 0, 'nginx_enabled': False,
                'authentication_cutover_enabled': False}))
            return
        if os.geteuid() != 0 or args.confirm_run != plan['run_id']:
            raise ValueError('root and explicit matching run confirmation required')
        from .staging_preflight import metadata
        if metadata() != {'id': SOURCE, 'ip': plan['source']['private_ip']}:
            raise ValueError('not the existing staging V1 source host')
        with operation_lock('/run/lock/yeodam-request-cutover.lock'):
            if args.command in ('install', 'start'):
                if args.report is None:
                    raise ValueError('host preflight report required')
                require_ready(plan, read_json(args.report))
            if args.command == 'install':
                if directory.exists() or state.exists():
                    raise ValueError('run already installed; never overwrite journal/state')
                tag = 'yeodam-request-cutover:sha-' + record['release']
                image = json.loads(run('docker', 'image', 'inspect', tag))[0]
                if image['Config'].get('Labels', {}).get('org.opencontainers.image.revision') != record['release']:
                    raise ValueError('load the image built from the exact Cloud release')
                BASE.mkdir(parents=True, exist_ok=True, mode=0o700)
                STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copytree(args.bundle, directory)
                state.mkdir(mode=0o700)
                from .config import private_file
                private_file(directory / 'runtime.env', f'CUTOVER_GATE_IMAGE={image["Id"]}\n'
                    f'CUTOVER_CONFIG_DIR={directory}\nCUTOVER_STATE_DIR={state}\n')
                compose(directory, 'config', '--quiet')
                print(json.dumps({'installed': True, 'started': False, 'nginx_changed': False}))
            elif args.command == 'start':
                verify_bundle(directory)
                # Avoid Compose silently recreating a running service or occupying another run's listener.
                for port in (19091, 19092):
                    with socket.socket() as probe:
                        probe.settimeout(1)
                        if probe.connect_ex(('127.0.0.1', port)) == 0:
                            raise ValueError('a gate/metrics listener is already running')
                compose(directory, 'up', '-d', '--no-build')
                for _ in range(30):
                    try:
                        current = status(directory, state)
                        with build_opener(ProxyHandler({})).open('http://127.0.0.1:19092/metrics', timeout=1) as response:
                            metrics_ready = b'yeodam_request_cutover_exporter_up 1\n' in response.read()
                        if current.get('run_id') == plan['run_id'] and current.get('phase') == 'V1' and metrics_ready:
                            break
                    except (OSError, ValueError, http.client.HTTPException):
                        pass
                    time.sleep(0.2)
                else:
                    compose(directory, 'stop', '--timeout', '10')
                    raise ValueError('new passthrough gate did not become ready; Nginx unchanged')
                print(json.dumps({'started': True, 'phase': 'V1', 'nginx_changed': False}))
            elif args.command == 'status':
                print(json.dumps(status(directory, state)))
            else:
                current = status(directory, state)
                if current.get('run_id') != plan['run_id']:
                    raise ValueError('gate belongs to a different run')
                require_detached(run('nginx', '-T'), current)
                compose(directory, 'stop', '--timeout', '620')
                print(json.dumps({'stopped': True, 'nginx_changed': False}))
    except Exception as error:
        # Do not print raw Docker/driver output: it may include environment values.
        print(json.dumps({'result': 'failed', 'error_type': type(error).__name__,
                          'message': str(error) if type(error) is ValueError else 'inspect private diagnostics'}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
