#!/usr/bin/env python3
"""Dispatch one #313 phase to its dedicated host; prints only the SSM command ID."""

import argparse
import base64
import json
from pathlib import Path
import subprocess
import tempfile
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
KAFKA = 'i-0b86e517fda2cfafc'
DESTINATION = 'i-034eaddf749eab9f3'
SOURCE_DB = 'i-05d320e3a87200d5a'
GENERATOR = 'i-0c53b65eb35dd2b42'
CONNECTOR = ROOT / 'scripts/configure-v2-staging-cdc-live.py'
PREPARE = ROOT / 'scripts/prepare-v2-staging-cdc-live-destination.sh'
ATTEST = ROOT / 'scripts/attest-v2-staging-cdc-live.py'
TRIP_DB = ROOT / 'tests/load/scripts/cdc-live-trip-db.py'
REFRESH_SMOKE = ROOT / 'tests/load/scripts/check-v1-refresh-session.py'
TRIP_SCENARIO = ROOT / 'tests/load/scenarios/migration-trip-create.js'
TRIP_RUNNER = ROOT / 'tests/load/scripts/run-k6.sh'
TRIP_ATTEST = ROOT / 'tests/load/scripts/attest-migration-trip.py'
ORDERED_AUDIT = ROOT / 'scripts/audit-v2-staging-cdc-ordered-events.py'
SOURCE_CONFIG = ROOT / 'deploy/v2-staging/kafka/cdc-live313-source.json'
SINK_CONFIG = ROOT / 'deploy/v2-staging/kafka/cdc-live313-sink.json'
DDL = ROOT / 'deploy/v2-staging/kafka/cdc-live313-destination.sql'
PHASES = ('destination', 'topics', 'initial-source', 'sink-users', 'sink-trips',
          'sink-trip_regions', 'ordered-topic', 'pause-source',
          'pause-legacy-sinks', 'ordered-source', 'ordered-sink',
          'resume-source', 'status', 'attest-source', 'attest-destination',
          'resume-legacy-trips', 'pause-legacy-trips',
          'resume-legacy-trip_regions', 'pause-legacy-trip_regions',
          'trip-check-source', 'trip-check-destination',
          'trip-count-source', 'trip-count-destination',
          'trip-export-source', 'trip-export-destination',
          'trip-attest-source', 'trip-attest-destination',
          'auth-refresh-smoke', 'generator-deploy', 'ordered-audit')
ORIGINAL_PHASES = ('destination', 'topics', 'initial-source', 'sink-users',
                   'sink-trips', 'sink-trip_regions', 'status', 'attest-source',
                   'attest-destination', 'trip-check-source', 'trip-check-destination',
                   'trip-count-source', 'trip-count-destination',
                   'trip-export-source', 'trip-export-destination',
                   'trip-attest-source', 'trip-attest-destination',
                   'auth-refresh-smoke', 'generator-deploy')
ORIGINAL_SERVER_IDS = {'318': '5405', '32210': '5406', '32230': '5407',
                       '322100': '5408'}
FIXED_SERVER_IDS = {'323': '5409'}
SERVER_IDS = {**ORIGINAL_SERVER_IDS, **FIXED_SERVER_IDS}
FIXED_PHASES = ('destination', 'topics', 'initial-source', 'ordered-sink', 'status',
                'ordered-audit',
                'attest-source', 'attest-destination', 'trip-check-source',
                'trip-check-destination', 'trip-count-source',
                'trip-count-destination', 'trip-export-source',
                'trip-export-destination', 'trip-attest-source',
                'trip-attest-destination', 'auth-refresh-smoke', 'generator-deploy')


def append_file(commands, path, source, case='313'):
    content = source.read_bytes()
    if case != '313':
        content = content.replace(b'live313', f'live{case}'.encode())
        if source == SOURCE_CONFIG:
            old = b'"database.server.id": "5404"'
            if content.count(old) != 1:
                raise RuntimeError('unexpected #313 server ID contract')
            content = content.replace(old, ('"database.server.id": "' +
                                            SERVER_IDS[case] + '"').encode())
    encoded = base64.b64encode(content).decode('ascii')
    commands.append(f": > {path}.b64")
    for start in range(0, len(encoded), 1000):
        commands.append(f"printf '%s' '{encoded[start:start + 1000]}' >> {path}.b64")
    commands.append(f'base64 --decode {path}.b64 > {path}')
    commands.append(f'rm {path}.b64')


def command_for(phase, run_id=None, case='313'):
    if (case not in ('313', *SERVER_IDS) or
            (case in ORIGINAL_SERVER_IDS and phase not in ORIGINAL_PHASES) or
            (case in FIXED_SERVER_IDS and phase not in FIXED_PHASES) or
            (phase == 'ordered-audit' and case != '323')):
        raise ValueError('case or phase is outside the fixed CDC experiment contract')
    commands = ['set -eu', 'umask 077']
    if case != '313':
        commands.append(f'export CDC_LIVE_CASE={case}')

    def transfer(path, source):
        append_file(commands, path, source, case)

    def result(instance):
        if case != '313':
            # Base64 chunks are opaque; rewrite their destination path only.
            rendered = []
            for line in commands:
                if line.startswith("printf '%s' '"):
                    payload, separator, destination = line.partition(' >> ')
                    if not separator:
                        raise RuntimeError('unexpected encoded transfer command')
                    rendered.append(payload + separator + destination.replace('live313', f'live{case}'))
                else:
                    rendered.append(line.replace('live313', f'live{case}'))
            return instance, rendered
        return instance, commands

    if phase == 'generator-deploy':
        base = '/opt/yeodam-load/source/tests/load'
        for filename, source in (
            ('scenarios/migration-trip-create.js', TRIP_SCENARIO),
            ('scripts/run-k6.sh', TRIP_RUNNER),
            ('scripts/attest-migration-trip.py', TRIP_ATTEST),
        ):
            temporary = '/run/yeodam-cdc-live313-' + Path(filename).name
            transfer(temporary, source)
            mode = '0755' if filename == 'scripts/run-k6.sh' else '0644'
            commands.append(f'install -o ubuntu -g ubuntu -m {mode} {temporary} {base}/{filename}')
            commands.append(f'sha256sum {base}/{filename}')
        return result(GENERATOR)
    if phase == 'destination':
        transfer('/run/yeodam-cdc-live313-destination.sh', PREPARE)
        transfer('/run/yeodam-cdc-live313-destination.sql.b64data', DDL)
        commands.append('export CDC_LIVE_DDL_B64="$(base64 -w0 /run/yeodam-cdc-live313-destination.sql.b64data)"')
        commands.append('bash /run/yeodam-cdc-live313-destination.sh')
        return result(DESTINATION)

    if phase.startswith('attest-'):
        role = phase.removeprefix('attest-')
        transfer('/run/yeodam-cdc-live313-attest.py', ATTEST)
        commands.append(f'python3 /run/yeodam-cdc-live313-attest.py {role}')
        return result(SOURCE_DB if role == 'source' else DESTINATION)

    if phase.startswith('trip-'):
        if not run_id or str(UUID(run_id)) != run_id:
            raise ValueError('trip phase requires a canonical run ID')
        _, action, role = phase.split('-')
        if action == 'attest':
            transfer('/run/yeodam-cdc-live313-trip-attest.py', TRIP_ATTEST)
            commands.append('python3 /run/yeodam-cdc-live313-trip-attest.py '
                            f'--kind rows --run-id {run_id} '
                            f'--path /run/yeodam-migration-trip/{run_id}.{role}.tsv.gz')
            return result(SOURCE_DB if role == 'source' else DESTINATION)
        transfer('/run/yeodam-cdc-live313-trip-db.py', TRIP_DB)
        commands.append(f'python3 /run/yeodam-cdc-live313-trip-db.py {action} '
                        f'--role {role} --run-id {run_id}')
        return result(SOURCE_DB if role == 'source' else DESTINATION)

    if phase == 'auth-refresh-smoke':
        if not run_id or str(UUID(run_id)) != run_id:
            raise ValueError('auth refresh smoke requires a canonical run ID')
        transfer('/run/yeodam-cdc-live313-refresh-smoke.py', REFRESH_SMOKE)
        commands.append(f'python3 /run/yeodam-cdc-live313-refresh-smoke.py --run-id {run_id}')
        return result(SOURCE_DB)

    if phase == 'ordered-audit':
        transfer('/run/yeodam-cdc-live323-ordered-audit.py', ORDERED_AUDIT)
        commands.append('python3 /run/yeodam-cdc-live323-ordered-audit.py')
        return result(KAFKA)

    transfer('/run/yeodam-cdc-live313.py', CONNECTOR)
    transfer('/run/yeodam-cdc-live313-source.json', SOURCE_CONFIG)
    transfer('/run/yeodam-cdc-live313-sink.json', SINK_CONFIG)
    commands.append('export CDC_LIVE_SOURCE_B64="$(base64 -w0 /run/yeodam-cdc-live313-source.json)"')
    commands.append('export CDC_LIVE_SINK_B64="$(base64 -w0 /run/yeodam-cdc-live313-sink.json)"')
    commands.append(f'python3 /run/yeodam-cdc-live313.py {phase}')
    return result(KAFKA)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=PHASES)
    parser.add_argument('--profile', default='yeodam-admin')
    parser.add_argument('--region', default='ap-northeast-2')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--run-id')
    parser.add_argument('--case', choices=('313', *SERVER_IDS), default='313')
    args = parser.parse_args()
    instance, commands = command_for(args.phase, args.run_id, args.case)
    if args.dry_run:
        print(args.phase, instance, 'command_lines', len(commands),
              'max_line_bytes', max(len(command.encode()) for command in commands))
        return
    with tempfile.NamedTemporaryFile(mode='w', prefix=f'yeodam-cdc-live{args.case}-',
                                     suffix='.json', delete=False) as stream:
        path = Path(stream.name)
        path.chmod(0o600)
        json.dump({'commands': commands}, stream)
    try:
        result = subprocess.run([
            'aws', 'ssm', 'send-command', '--profile', args.profile,
            '--region', args.region, '--instance-ids', instance,
            '--document-name', 'AWS-RunShellScript', '--parameters', 'file://' + str(path),
            '--query', 'Command.CommandId', '--output', 'text',
        ], check=True, capture_output=True, text=True)
    finally:
        path.unlink(missing_ok=True)
    print(args.phase, instance, result.stdout.strip())


if __name__ == '__main__':
    main()
