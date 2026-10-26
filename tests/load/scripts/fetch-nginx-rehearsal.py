#!/usr/bin/env python3
"""Pull redacted archives through one-shot SSM tunnels, then export the same CloudWatch window."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import subprocess
import tarfile
import time
import urllib.request
from uuid import UUID

spec = importlib.util.spec_from_file_location('archive', Path(__file__).with_name('nginx-rehearsal-archive.py'))
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)
OPS = '/opt/yeodam/ops/nginx-rehearsal-426/nginx-rehearsal-archive.py'


def extract(path, destination):
    with tarfile.open(path) as tar:
        members = tar.getmembers()
        if len(members) > 100 or sum(x.size for x in members) > 1024**3:
            raise ValueError('Archive exceeds extraction budget')
        for member in members:
            if not member.isfile() or Path(member.name).name != member.name:
                raise ValueError('Only flat regular evidence files are allowed')
            with tar.extractfile(member) as source, (destination/member.name).open('xb') as output:
                while chunk := source.read(1024*1024):
                    output.write(chunk)
    checksums = json.loads((destination/'checksums.json').read_text())
    if set(checksums) != {p.name for p in destination.iterdir()}-{'checksums.json'}:
        raise ValueError('Archive membership differs from manifest')
    for name, expected in checksums.items():
        if archive.file_hash(destination/name) != expected:
            raise ValueError('Checksum mismatch')


def cloudwatch(call, group, start, end, output, key, max_pages=2000):
    token = None
    seen = set()
    count = 0
    for _ in range(max_pages):
        args = ['logs', 'filter-log-events', '--no-paginate', '--log-group-name', group,
                '--start-time', str(start), '--end-time', str(end), '--limit', '1000']
        if token:
            args += ['--next-token', token]
        data = call(args)
        for event in data.get('events', []):
            identity = event['eventId']
            if identity in seen:
                raise ValueError('Duplicate CloudWatch event ID')
            seen.add(identity)
            try:
                body = json.loads(event['message'])
                body = archive.scrub(body, key)
            except ValueError:
                body = {'nonJsonMessageSha256': hashlib.sha256(event['message'].encode()).hexdigest()}
            output.write(json.dumps({'eventId': identity, 'timestamp': event['timestamp'],
                'ingestionTime': event.get('ingestionTime'), 'data': body})+'\n')
            count += 1
        next_token = data.get('nextToken')
        if not next_token:
            return count
        if next_token == token:
            raise ValueError('Non-advancing CloudWatch token')
        token = next_token  # Empty pages are not end-of-results.
    raise ValueError('CloudWatch page budget exceeded')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id', required=True)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--profile', default='yeodam-admin')
    p.add_argument('--from', dest='start')
    p.add_argument('--to', dest='end')
    p.add_argument('--loki-lookahead-seconds', type=int, default=60)
    a = p.parse_args()
    UUID(a.run_id)
    if not 0 <= a.loki_lookahead_seconds <= 3600:
        p.error('Loki padding must be0..3600s')
    if bool(a.start) != bool(a.end):
        p.error('Historical recovery requires --from and --to together')
    os.umask(0o077)
    a.output.mkdir(parents=True, exist_ok=False)
    aws = ['aws', '--profile', a.profile, '--region', 'ap-northeast-2']
    def call(args):
        return json.loads(subprocess.check_output(aws+args+['--output', 'json'], text=True))
    if call(['sts', 'get-caller-identity'])['Account'] != '483175530259':
        raise ValueError('Unexpected AWS account')
    def send(instance, command):
        return call(['ssm', 'send-command', '--instance-ids', instance, '--document-name', 'AWS-RunShellScript',
            '--comment', 'Cloud426 evidence export', '--parameters', json.dumps({'commands':[command], 'executionTimeout':['900']})])['Command']['CommandId']
    def remote(instance, command):
        cid = send(instance, command)
        requested = time.monotonic()
        deadline = requested+910
        while time.monotonic() < deadline:
            try:
                result = call(['ssm', 'get-command-invocation', '--instance-id', instance, '--command-id', cid])
            except subprocess.CalledProcessError:
                if time.monotonic()-requested > 10:
                    raise
                time.sleep(2)
                continue
            if result['Status'] == 'Success':
                return json.loads(result['StandardOutputContent'])
            if result['Status'] not in ['Pending', 'InProgress', 'Delayed']:
                raise RuntimeError('Remote export failed: '+cid)
            time.sleep(2)
        raise TimeoutError('Remote archive deadline: '+cid)
    def pull(instance, info, name):
        folder = a.output/name
        folder.mkdir()
        if not re.fullmatch(r'/var/lib/yeodam/nginx-rehearsal(?:-plg)?/[0-9a-f-]+/(?:exports/)?[0-9a-f-]+\.tar\.gz', info['archive']):
            raise ValueError('Unexpected archive identity')
        if info['size'] > 512*1024*1024:
            raise ValueError('Archive download exceeds budget')
        with socket.socket() as check:
            check.bind(('127.0.0.1', 0))
            local_port = check.getsockname()[1]
        cid = send(instance, 'python3 '+OPS+' serve --archive '+info['archive'])
        log_path = a.output/(name+'-tunnel.log')
        with log_path.open('w') as log:
            tunnel = subprocess.Popen(aws+['ssm', 'start-session', '--target', instance,
                '--document-name', 'AWS-StartPortForwardingSession', '--parameters',
                json.dumps({'portNumber':['19426'], 'localPortNumber':[str(local_port)]})], stdout=log, stderr=log, start_new_session=True)
            try:
                deadline = time.monotonic()+20
                while ('Port '+str(local_port)+' opened') not in log_path.read_text():
                    if tunnel.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('SSM forwarding failed')
                    time.sleep(.2)
                time.sleep(1)
                target = a.output/(name+'.tar.gz')
                total = 0
                with urllib.request.urlopen('http://127.0.0.1:'+str(local_port)+'/archive', timeout=60) as source, target.open('xb') as out:
                    while chunk := source.read(1024*1024):
                        total += len(chunk)
                        if total > info['size']:
                            raise ValueError('Unexpected download length')
                        out.write(chunk)
                if total != info['size'] or archive.file_hash(target) != info['sha256']:
                    raise ValueError('Transfer checksum differs')
                extract(target, folder)
            finally:
                os.killpg(tunnel.pid, signal.SIGTERM) if tunnel.poll() is None else None
                tunnel.wait(timeout=10)
                match = re.search(r'SessionId:\s*([\w-]+)', log_path.read_text())
                if match:
                    call(['ssm', 'terminate-session', '--session-id', match[1]])
        return {'transferCommandId': cid, **info}
    transfers = {}
    if a.start:
        index = {'from':a.start,'to':a.end}
    else:
        app_info = remote(archive.APP, 'python3 '+OPS+' app --run-id '+a.run_id)
        transfers['app'] = pull(archive.APP, app_info, 'app')
        index = json.loads((a.output/'app/index.json').read_text())
    plg_info = remote(archive.PLG, shlex.join(['python3', OPS, 'plg', '--run-id', a.run_id,
        '--from', index['from'], '--to', index['to'], '--loki-lookahead-seconds', str(a.loki_lookahead_seconds)]))
    transfers['plg'] = pull(archive.PLG, plg_info, 'plg')
    result = {'runId': a.run_id, 'from':index['from'],'to':index['to'],'hostCaptureAvailable':not bool(a.start), 'transfers': transfers, 'cloudwatch': {},
              'note': 'No final user-success claim; attach normal probe, journey and DB/S3 audit results'}
    import secrets
    key = secrets.token_bytes(32)
    for name in ['backend', 'ai']:
        with (a.output/('cloudwatch-'+name+'.jsonl')).open('w') as out:
            try:
                count = cloudwatch(call, '/yeodam/v2/staging/v1-source/'+name,
                    archive.ns(index['from'])//1_000_000, archive.ns(index['to'])//1_000_000+1, out, key)
                result['cloudwatch'][name] = {'events': count, 'complete': True}
            except Exception as error:
                result['cloudwatch'][name] = {'complete': False, 'errorType': type(error).__name__}
    result['exportedAt'] = archive.utc()
    archive.dump(a.output/'run-index.json', result)
    print(json.dumps({'runId': a.run_id, 'folder': str(a.output), 'cloudwatch': result['cloudwatch']}))


if __name__ == '__main__':
    main()
