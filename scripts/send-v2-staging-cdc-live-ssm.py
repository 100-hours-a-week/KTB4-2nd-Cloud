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
TRIP_ATTEST = ROOT / 'tests/load/scripts/attest-migration-trip.py'
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
          'auth-refresh-smoke', 'generator-deploy')


def append_file(commands, path, source):
    encoded = base64.b64encode(source.read_bytes()).decode('ascii')
    commands.append(f": > {path}.b64")
    for start in range(0, len(encoded), 1000):
        commands.append(f"printf '%s' '{encoded[start:start + 1000]}' >> {path}.b64")
    commands.append(f'base64 --decode {path}.b64 > {path}')
    commands.append(f'rm {path}.b64')


def command_for(phase, run_id=None):
    commands = ['set -eu', 'umask 077']
    if phase == 'generator-deploy':
        base = '/opt/yeodam-load/source/tests/load'
        for filename, source in (
            ('scenarios/migration-trip-create.js', TRIP_SCENARIO),
            ('scripts/attest-migration-trip.py', TRIP_ATTEST),
        ):
            temporary = '/run/yeodam-cdc-live313-' + Path(filename).name
            append_file(commands, temporary, source)
            commands.append(f'install -o ubuntu -g ubuntu -m 0644 {temporary} {base}/{filename}')
            commands.append(f'sha256sum {base}/{filename}')
        return GENERATOR, commands
    if phase == 'destination':
        append_file(commands, '/run/yeodam-cdc-live313-destination.sh', PREPARE)
        append_file(commands, '/run/yeodam-cdc-live313-destination.sql.b64data', DDL)
        commands.append('export CDC_LIVE_DDL_B64="$(base64 -w0 /run/yeodam-cdc-live313-destination.sql.b64data)"')
        commands.append('bash /run/yeodam-cdc-live313-destination.sh')
        return DESTINATION, commands

    if phase.startswith('attest-'):
        role = phase.removeprefix('attest-')
        append_file(commands, '/run/yeodam-cdc-live313-attest.py', ATTEST)
        commands.append(f'python3 /run/yeodam-cdc-live313-attest.py {role}')
        return (SOURCE_DB if role == 'source' else DESTINATION), commands

    if phase.startswith('trip-'):
        if not run_id or str(UUID(run_id)) != run_id:
            raise ValueError('trip phase requires a canonical run ID')
        _, action, role = phase.split('-')
        if action == 'attest':
            append_file(commands, '/run/yeodam-cdc-live313-trip-attest.py', TRIP_ATTEST)
            commands.append('python3 /run/yeodam-cdc-live313-trip-attest.py '
                            f'--kind rows --run-id {run_id} '
                            f'--path /run/yeodam-migration-trip/{run_id}.{role}.tsv.gz')
            return (SOURCE_DB if role == 'source' else DESTINATION), commands
        append_file(commands, '/run/yeodam-cdc-live313-trip-db.py', TRIP_DB)
        commands.append(f'python3 /run/yeodam-cdc-live313-trip-db.py {action} '
                        f'--role {role} --run-id {run_id}')
        return (SOURCE_DB if role == 'source' else DESTINATION), commands

    if phase == 'auth-refresh-smoke':
        if not run_id or str(UUID(run_id)) != run_id:
            raise ValueError('auth refresh smoke requires a canonical run ID')
        append_file(commands, '/run/yeodam-cdc-live313-refresh-smoke.py', REFRESH_SMOKE)
        commands.append(f'python3 /run/yeodam-cdc-live313-refresh-smoke.py --run-id {run_id}')
        return SOURCE_DB, commands

    append_file(commands, '/run/yeodam-cdc-live313.py', CONNECTOR)
    append_file(commands, '/run/yeodam-cdc-live313-source.json', SOURCE_CONFIG)
    append_file(commands, '/run/yeodam-cdc-live313-sink.json', SINK_CONFIG)
    commands.append('export CDC_LIVE_SOURCE_B64="$(base64 -w0 /run/yeodam-cdc-live313-source.json)"')
    commands.append('export CDC_LIVE_SINK_B64="$(base64 -w0 /run/yeodam-cdc-live313-sink.json)"')
    commands.append(f'python3 /run/yeodam-cdc-live313.py {phase}')
    return KAFKA, commands


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=PHASES)
    parser.add_argument('--profile', default='yeodam-admin')
    parser.add_argument('--region', default='ap-northeast-2')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--run-id')
    args = parser.parse_args()
    instance, commands = command_for(args.phase, args.run_id)
    if args.dry_run:
        print(args.phase, instance, 'command_lines', len(commands),
              'max_line_bytes', max(len(command.encode()) for command in commands))
        return
    with tempfile.NamedTemporaryFile(mode='w', prefix='yeodam-cdc-live313-',
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
