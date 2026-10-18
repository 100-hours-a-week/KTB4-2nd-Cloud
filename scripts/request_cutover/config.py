"""Shared scope checks; configuration never contains credentials."""
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import UUID


def run_id(value):
    if str(UUID(value)) != value:
        raise ValueError('run_id must be a canonical UUID')
    return value


def origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username or parsed.password or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment):
        raise ValueError('upstream must be a fixed HTTP(S) origin')
    host = parsed.hostname
    if host in ('yeodam-2gether.com', 'staging.yeodam-2gether.com',
                'v1-staging.yeodam-2gether.com'):
        raise ValueError('shared/public application hosts are not rehearsal upstreams')
    return value.rstrip('/')


def load(path):
    c = json.loads(Path(path).read_text())
    run_id(c['run_id'])
    if c['environment'] != 'isolated-staging':
        raise ValueError('only isolated-staging is supported')
    if not re.fullmatch(r'yeodam_cutover_[a-z0-9_]{1,32}', c['mysql']['database']):
        raise ValueError('refuse shared, QA or production schema')
    if c['redis']['prefix'] != 'yeodam:cutover:' + c['run_id'] + ':':
        raise ValueError('Redis prefix must belong exclusively to this run')
    if c['source_timezone'] not in ('UTC', 'Asia/Seoul'):
        raise ValueError('source timezone must be explicitly verified')
    for key in ('v1_api', 'v1_web', 'v2_origin'):
        c[key] = origin(c[key])
    if len(set(c[k] for k in ('v1_api', 'v1_web', 'v2_origin'))) < 2:
        raise ValueError('V1 and V2 destinations must differ')
    if not 0 < c.get('hold_seconds', 5) <= 5:
        raise ValueError('hold_seconds must be in (0, 5]')
    for key, maximum in (('max_waiters', 1000), ('max_wait_bytes', 67108864)):
        if type(c.get(key, maximum)) is not int or not 1 <= c.get(key, maximum) <= maximum:
            raise ValueError('invalid hold capacity')
    for key in ('socket', 'journal'):
        if not Path(c[key]).is_absolute():
            raise ValueError(key + ' must be an absolute private path')
    return c


def password(section):
    name = section['password_env']
    if not re.fullmatch(r'CUTOVER_[A-Z_]+', name):
        raise ValueError('credential environment variable must use CUTOVER_ prefix')
    return os.environ[name]


def private_file(path, content):
    """Exclusive creation; never silently replace config/evidence."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)
