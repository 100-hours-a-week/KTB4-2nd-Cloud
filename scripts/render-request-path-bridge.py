#!/usr/bin/env python3
"""Validate and render a staging-only Nginx bridge; never contact or change servers."""
import argparse
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'deploy/request-path-bridge/bridge.conf.tmpl'
GATES = frozenset({
    'writer_and_schema', 'session_csrf_refresh', 'legacy_upload_and_assets',
    'pending_jobs', 'host_tls_oauth', 'rollback_with_current_writer',
})
FIELDS = frozenset({
    'schemaVersion', 'environment', 'public_host', 'alb_dns_name',
    'upstream_tls_name', 'dns_resolvers', 'certificate', 'certificate_key',
    'upstream_ca_bundle', 'max_body_megabytes', 'connect_timeout_seconds',
    'send_timeout_seconds', 'read_timeout_seconds', 'trip_timeout_seconds', 'readiness',
})


def hostname(value):
    if not isinstance(value, str) or len(value) > 253 or value != value.lower():
        raise ValueError('DNS names must be lower-case hostnames without URLs or ports')
    labels = value.split('.')
    if len(labels) < 2 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',
                                             label) for label in labels):
        raise ValueError('Invalid DNS hostname')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError('Use DNS, not an IP address')


def validate(config):
    if not isinstance(config, dict) or set(config) != FIELDS:
        raise ValueError('Configuration fields must match config.example.json exactly')
    if type(config['schemaVersion']) is not int or config['schemaVersion'] != 1:
        raise ValueError('Unsupported schemaVersion')
    if config['environment'] != 'staging':
        raise ValueError('This preparation tool only supports staging')
    public = hostname(config['public_host'])
    if (not public.endswith('.yeodam-2gether.com') or 'staging' not in public.split('.')[0]):
        raise ValueError('Use an explicit staging Host, not the production domain')
    alb = hostname(config['alb_dns_name'])
    if not re.fullmatch(r'[a-z0-9-]+\.ap-northeast-2\.elb\.amazonaws\.com', alb):
        raise ValueError('Use the dedicated Seoul ALB DNS name, not the public Host')
    hostname(config['upstream_tls_name'])
    resolvers = config['dns_resolvers']
    if not isinstance(resolvers, list) or not 1 <= len(resolvers) <= 3:
        raise ValueError('Supply one to three trusted DNS resolver IPv4 addresses')
    for resolver in resolvers:
        address = ipaddress.ip_address(resolver) if isinstance(resolver, str) else None
        if address is None or address.version != 4 or address.is_unspecified or address.is_multicast:
            raise ValueError('Invalid DNS resolver IPv4 address')
    for field in ('certificate', 'certificate_key', 'upstream_ca_bundle'):
        value = config[field]
        if (not isinstance(value, str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', value)
                or '..' in PurePosixPath(value).parts or value.endswith('/')):
            raise ValueError(f'{field} must be an absolute file path without config syntax')
    bounds = {
        'max_body_megabytes': (1, 150), 'connect_timeout_seconds': (1, 30),
        'send_timeout_seconds': (1, 600), 'read_timeout_seconds': (1, 600),
        'trip_timeout_seconds': (1, 600),
    }
    for field, (minimum, maximum) in bounds.items():
        value = config[field]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f'{field} must be an integer between {minimum} and {maximum}')
    if config['trip_timeout_seconds'] < max(config['send_timeout_seconds'],
                                            config['read_timeout_seconds']):
        raise ValueError('Trip timeout must not be shorter than the general timeouts')
    gates = config['readiness']
    if not isinstance(gates, dict) or set(gates) != GATES:
        raise ValueError('All readiness gates must be declared')
    for value in gates.values():
        if value is not None and (not isinstance(value, str) or not value.strip()
                                  or len(value) > 500 or any(ord(c) < 32 for c in value)):
            raise ValueError('Readiness must be null or a nonempty single-line evidence reference')
    return sorted(key for key, value in gates.items() if value is None)


def render(config, release, allow_unverified=False):
    pending = validate(config)
    if not re.fullmatch(r'[0-9a-f]{40}', release):
        raise ValueError('Cloud release must be a full commit SHA')
    if pending and not allow_unverified:
        raise ValueError('Unverified readiness: ' + ', '.join(pending))
    values = {key.upper(): str(value) for key, value in config.items()}
    values.update(RELEASE=release, DNS_RESOLVERS=' '.join(config['dns_resolvers']),
                  READINESS='UNVERIFIED / LOCAL CHECK ONLY' if pending else 'MANUALLY ATTESTED')
    template = TEMPLATE.read_text()
    result = re.sub(r'@@([A-Z_]+)@@', lambda match: values[match[1]], template)
    if '@@' in result:
        raise ValueError('Unresolved template marker')
    return result


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON field: {key}')
        result[key] = value
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check', 'render'))
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--release')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--allow-unverified', action='store_true',
                        help='Render ONLY for local nginx -t; does not approve a rehearsal')
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(), object_pairs_hook=unique_object)
        pending = validate(config)
        if args.command == 'check':
            print(json.dumps({'configuration_valid': True, 'pending': pending,
                              'manual_attestation_complete': not pending,
                              'runtime_verified_by_this_tool': False}, ensure_ascii=False))
            return 2 if pending else 0
        if args.release is None or args.output is None:
            raise ValueError('render requires --release and --output')
        content = render(config, args.release, args.allow_unverified)
        # Exclusive creation prevents overwriting the active config or a previous result.
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(content)
        print('Rendered only; no server change. Pending gates: ' + str(len(pending)))
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
