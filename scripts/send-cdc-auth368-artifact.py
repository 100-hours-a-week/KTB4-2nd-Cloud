#!/usr/bin/env python3
"""Place public #368 code over SSM; never transfer authentication values."""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'i-05d320e3a87200d5a'
KAFKA = 'i-0b86e517fda2cfafc'
ARTIFACTS = {
    'bridge-source': (SOURCE, [(ROOT / 'scripts/cdc-auth368-key-bridge.py', '/run/yeodam-cdc-auth368-key-bridge.py', '0600')]),
    'bridge-kafka': (KAFKA, [(ROOT / 'scripts/cdc-auth368-key-bridge.py', '/run/yeodam-cdc-auth368-key-bridge.py', '0600')]),
    'plugin': (KAFKA, [(Path('/tmp/yeodam-cdc-auth368.jar'), '/opt/yeodam-kafka/cdc-libs/yeodam-auth368-smt.jar', '0644')]),
    'consumer': (SOURCE, [
        (ROOT / 'scripts/cdc_auth368.py', '/opt/yeodam-cdc-auth368/cdc_auth368.py', '0644'),
        (ROOT / 'scripts/run-cdc-auth368.py', '/opt/yeodam-cdc-auth368/run-cdc-auth368.py', '0644'),
        (ROOT / 'scripts/prepare-cdc-auth368-consumer.py', '/opt/yeodam-cdc-auth368/prepare-cdc-auth368-consumer.py', '0644'),
        (ROOT / 'scripts/cdc-auth368-api-smoke.py', '/opt/yeodam-cdc-auth368/api-smoke.py', '0644'),
        (ROOT / 'scripts/audit-cdc-auth368.py', '/opt/yeodam-cdc-auth368/audit-cdc-auth368.py', '0644'),
        (ROOT / 'scripts/requirements-cdc-auth368.txt', '/opt/yeodam-cdc-auth368/requirements.txt', '0644'),
    ]),
    'source-config': (KAFKA, [(ROOT / 'deploy/v2-staging/kafka/cdc-auth368-source.json',
                               '/run/yeodam-cdc-auth368-source.json', '0600')]),
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('artifact', choices=ARTIFACTS)
    p.add_argument('--profile', default='yeodam-admin')
    a = p.parse_args()
    instance, files = ARTIFACTS[a.artifact]
    commands = ['set -eu', 'umask 077']
    for index, (source, target, mode) in enumerate(files):
        encoded = base64.b64encode(source.read_bytes()).decode()
        temporary = f'/run/yeodam-cdc-auth368-artifact-{index}.b64'
        commands.append(f': > {temporary}')
        for start in range(0, len(encoded), 1000):
            commands.append(f"printf '%s' '{encoded[start:start + 1000]}' >> {temporary}")
        commands.append(f'install -d -m 0755 {Path(target).parent}')
        commands.append(f'base64 -d {temporary} > {target}.new')
        commands.append(f'install -m {mode} {target}.new {target}')
        commands.append(f'rm {temporary} {target}.new')
        commands.append(f'sha256sum {target}')
    request = {'InstanceIds': [instance], 'DocumentName': 'AWS-RunShellScript',
               'Parameters': {'commands': commands}}
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=True) as f:
        json.dump(request, f)
        f.flush()
        result = subprocess.run(['aws', 'ssm', 'send-command', '--profile', a.profile,
                                 '--region', 'ap-northeast-2', '--cli-input-json',
                                 'file://' + f.name, '--query', 'Command.CommandId',
                                 '--output', 'text'], check=True, text=True, capture_output=True)
    print(result.stdout.strip())


if __name__ == '__main__':
    main()
