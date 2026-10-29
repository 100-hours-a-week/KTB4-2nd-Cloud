#!/usr/bin/env python3
"""Render a local candidate only; never install, reload Nginx or contact AWS."""
import argparse
import hashlib
from pathlib import Path

from nginx_read_enforcement import ENFORCED_PREFIX, ENFORCED_SERVER
from nginx_create_enforcement import ENFORCED_LOCATION


def candidate(site, ip_rate, ip_burst):
    if not 6 <= ip_rate <= 100000 or not 30 <= ip_burst <= 100000:
        raise ValueError('Explicit reviewed IP rate >5 and burst >=30 are required; no defaults are selected')
    if (site.count('server_name v1-staging.yeodam-2gether.com;') != 2
            or site.count(ENFORCED_PREFIX) != 1 or site.count(ENFORCED_SERVER) != 1
            or site.count(ENFORCED_LOCATION) != 1):
        raise ValueError('Expected the exact V1 staging enforced read and creation policies')
    # Preserve route maps, auth exclusions, creation limits, headers and every other byte.
    prefix = ENFORCED_PREFIX.replace('rate=5r/s;', f'rate={ip_rate}r/s;')
    server = ENFORCED_SERVER.replace('burst=30 nodelay;', f'burst={ip_burst} nodelay;')
    return site.replace(ENFORCED_PREFIX, prefix, 1).replace(ENFORCED_SERVER, server, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True, type=Path)
    parser.add_argument('--expected-site-sha256', required=True)
    parser.add_argument('--ip-rate', required=True, type=int)
    parser.add_argument('--ip-burst', required=True, type=int)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.site.resolve() == args.output.resolve():
        parser.error('Candidate must not overwrite the input site')
    original = args.site.read_bytes()
    if hashlib.sha256(original).hexdigest() != args.expected_site_sha256:
        parser.error('Site checksum differs; inspect the current source')
    result = candidate(original.decode(), args.ip_rate, args.ip_burst)
    # Exclusive creation prevents an unnoticed overwrite of another candidate or system file.
    with args.output.open('x') as output:
        output.write(result)
    print('Local candidate created; backend readiness, IP capacity and deployment are not verified')


if __name__ == '__main__':
    main()
