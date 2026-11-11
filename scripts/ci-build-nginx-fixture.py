#!/usr/bin/env python3
"""Build a public CI fixture; diagnose host/build DNS before restricted recovery."""
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'yeodam-nginx-jwt-468:local'
DOMAINS = ('archive.ubuntu.com', 'security.ubuntu.com')


def host_dns():
    results = {}
    for domain in DOMAINS:
        try:
            results[domain] = bool(socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM))
        except OSError:
            results[domain] = False
    return results


def build(run=subprocess.run, resolve=host_dns, env=None, system=None):
    env = os.environ if env is None else env
    system = platform.system() if system is None else system
    source = (ROOT / 'tests/ci/nginx-jwt-gate.Dockerfile').read_text()
    base = re.search(r'^FROM (ubuntu@sha256:[a-f0-9]{64})$', source, re.M)
    if not base:
        raise ValueError('Pinned public fixture base required')
    command = ['docker', 'build', '--progress=plain', '--platform', 'linux/amd64', '-t', IMAGE, '-']
    # This Dockerfile has no COPY. stdin excludes the checkout and all local files.
    def execute(argv, text):
        return run(argv, input=text, text=True, capture_output=True, timeout=240)
    first = execute(command, source)
    if first.returncode == 0:
        return {'buildNetwork': 'default', 'recovered': False}
    sys.stderr.write(first.stdout + first.stderr)
    dns_failure = 'could not resolve' in (first.stdout + first.stderr).lower()
    if not dns_failure:
        raise RuntimeError('Fixture build failed outside DNS recovery scope')
    hosts = resolve()
    print(json.dumps({'hostDns': hosts, 'defaultBuildDnsFailed': True}), file=sys.stderr)
    if not all(hosts.values()) or env.get('GITHUB_ACTIONS') != 'true' or env.get('RUNNER_OS') != 'Linux' or env.get('RUNNER_ENVIRONMENT') != 'github-hosted' or system != 'Linux':
        raise RuntimeError('Build DNS failed; host recovery requires healthy DNS on a hosted Linux CI runner')
    probe = 'FROM ' + base[1] + '\nRUN cat /etc/resolv.conf && getent hosts archive.ubuntu.com && getent hosts security.ubuntu.com\n'
    probe_command = ['docker', 'build', '--progress=plain', '--platform', 'linux/amd64', '--no-cache', '--network=host', '-']
    diagnosis = execute(probe_command, probe)
    sys.stderr.write(diagnosis.stdout + diagnosis.stderr)
    if diagnosis.returncode:
        raise RuntimeError('Host-network build DNS probe failed; no package build retry')
    recovery = execute(command[:-1] + ['--network=host', '-'], source)
    if recovery.returncode:
        sys.stderr.write(recovery.stdout + recovery.stderr)
        raise RuntimeError('One host-network fixture build retry failed')
    return {'buildNetwork': 'host', 'recovered': True, 'runtimeNetwork': 'none'}


if __name__ == '__main__':
    print(json.dumps(build()))
