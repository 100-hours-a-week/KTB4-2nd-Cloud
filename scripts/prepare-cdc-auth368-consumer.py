#!/usr/bin/env python3
"""Create a private, run-scoped Redis destination without embedding credentials."""
import argparse
import json
import os
from pathlib import Path
from uuid import UUID


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run-id', required=True)
    p.add_argument('--redis-host', required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    if str(UUID(a.run_id)) != a.run_id:
        raise ValueError('canonical Run UUID required')
    if not a.redis_host.startswith('master.yeodam-v2-staging-redis.') or not a.redis_host.endswith('.cache.amazonaws.com'):
        raise ValueError('unexpected staging Redis endpoint')
    output = Path(a.output)
    if not output.is_absolute() or output.parent != Path('/run/yeodam-cdc-auth368'):
        raise ValueError('private runtime path required')
    config = {'run_id': a.run_id, 'source_timezone': 'Asia/Seoul',
              'redis': {'host': a.redis_host, 'port': 6379, 'db': 0, 'tls': True,
                        'password_env': 'CUTOVER_REDIS_PASSWORD',
                        'prefix': 'yeodam:cutover:' + a.run_id + ':'}}
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(config, stream, separators=(',', ':'))
    print('AUTH368_CONFIG_READY ' + a.run_id)


if __name__ == '__main__':
    main()
