#!/usr/bin/env python3
"""Build a public CI fixture; diagnose host/build DNS before restricted recovery."""
import json
import ipaddress
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import urllib.request
import urllib.error

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


def parse_addresses(domain, data):
    if domain not in DOMAINS or data.get('Status') != 0 or data.get('TC'):
        raise ValueError('Unexpected HTTPS DNS response')
    question = data.get('Question', [])
    if len(question) != 1 or question[0].get('name', '').rstrip('.') != domain or question[0].get('type') != 1:
        raise ValueError('HTTPS DNS question differs')
    addresses = []
    for row in data.get('Answer', []):
        if row.get('type') == 1 and row.get('name', '').rstrip('.') == domain:
            address = ipaddress.ip_address(row['data'])
            if address.version != 4 or not address.is_global or row.get('TTL', 0) <= 0:
                raise ValueError('HTTPS DNS returned a non-public or expired address')
            if str(address) not in addresses:
                addresses.append(str(address))
    if not addresses:
        raise ValueError('No exact public Ubuntu A record')
    return addresses[:4]


RESOLVERS = ('https://dns.google/resolve', 'https://cloudflare-dns.com/dns-query')


def https_query(endpoint, domain):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    resolver = urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(endpoint + '?name=' + domain + '&type=A&edns_client_subnet=0.0.0.0/0', headers={'Accept': 'application/dns-json'})
    with resolver.open(request, timeout=10) as response:
        body = response.read(32769)
        if response.status != 200 or len(body) > 32768:
            raise ValueError('HTTPS DNS response unavailable or too large')
    return json.loads(body)


def https_addresses(query=https_query):
    result = {}
    for domain in DOMAINS:
        for endpoint in RESOLVERS:
            try:
                data = query(endpoint, domain)
            except urllib.error.HTTPError as error:
                if error.code != 429 and error.code < 500:
                    raise
                print(json.dumps({'dnsHost': domain, 'resolver': endpoint, 'httpStatus': error.code}), file=sys.stderr)
                continue
            except (urllib.error.URLError, TimeoutError):
                print(json.dumps({'dnsHost': domain, 'resolver': endpoint, 'transportFailed': True}), file=sys.stderr)
                continue
            # SERVFAIL and truncation are availability failures. NXDOMAIN,
            # wrong questions/private IPs/malformed data remain hard failures.
            if data.get('Status') == 2 or data.get('TC') is True:
                print(json.dumps({'dnsHost': domain, 'resolver': endpoint, 'dnsStatus': data.get('Status'), 'truncated': data.get('TC', False)}), file=sys.stderr)
                continue
            result[domain] = parse_addresses(domain, data)
            print(json.dumps({'dnsHost': domain, 'resolverUsed': endpoint}), file=sys.stderr)
            break
        if domain not in result:
            raise RuntimeError('Both reviewed HTTPS DNS resolvers unavailable')
    return result


def build(run=subprocess.run, resolve=host_dns, env=None, system=None, resolve_https=https_addresses):
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
    if env.get('GITHUB_ACTIONS') != 'true' or env.get('RUNNER_OS') != 'Linux' or env.get('RUNNER_ENVIRONMENT') != 'github-hosted' or system != 'Linux':
        raise RuntimeError('Build DNS recovery requires a hosted Linux CI runner')
    probe = 'FROM ' + base[1] + '\nRUN cat /etc/resolv.conf && getent hosts archive.ubuntu.com && getent hosts security.ubuntu.com\n'
    if all(hosts.values()):
        flags = ['--network=host']
        mode = 'host'
    else:
        addresses = resolve_https()
        if set(addresses) != set(DOMAINS):
            raise ValueError('Only two fixed Ubuntu build hosts are permitted')
        flags = []
        for domain in DOMAINS:
            # Revalidate injected/test results too; never bind private infrastructure.
            validated = parse_addresses(domain, {'Status': 0, 'Question': [{'name': domain + '.', 'type': 1}], 'Answer': [{'name': domain + '.', 'type': 1, 'TTL': 1, 'data': ip} for ip in addresses[domain]]})
            for address in validated:
                flags += ['--add-host', domain + '=' + address]
        mode = 'https-dns-build-hosts'
        print(json.dumps({'httpsDnsPolicy': 'fixed-public-resolvers', 'buildHosts': list(DOMAINS), 'globalResolverChanged': False}), file=sys.stderr)
    probe_command = ['docker', 'build', '--progress=plain', '--platform', 'linux/amd64', '--no-cache'] + flags + ['-']
    diagnosis = execute(probe_command, probe)
    sys.stderr.write(diagnosis.stdout + diagnosis.stderr)
    if diagnosis.returncode:
        raise RuntimeError('Scoped build DNS probe failed; no package build retry')
    recovery = execute(command[:-1] + flags + ['-'], source)
    if recovery.returncode:
        sys.stderr.write(recovery.stdout + recovery.stderr)
        raise RuntimeError('One scoped fixture build retry failed')
    return {'buildNetwork': mode, 'recovered': True, 'runtimeNetwork': 'none'}


if __name__ == '__main__':
    print(json.dumps(build()))
