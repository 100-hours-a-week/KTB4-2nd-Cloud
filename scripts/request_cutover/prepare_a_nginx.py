#!/usr/bin/env python3
"""Prepare an exact V1 staging Nginx Gate candidate without activating it."""
import hashlib
import os
from pathlib import Path

SOURCE = Path('/etc/nginx/sites-available/yeodam-v1-staging')
RUN_DIR = Path('/var/lib/yeodam/a-rehearsal-358')
EXPECTED = '5f4d61ea8924e1245595867781925d2e208c1fab12f4357f74968b33d7d283d5'


def main():
    if os.geteuid() != 0 or not SOURCE.is_file() or SOURCE.is_symlink():
        raise ValueError('run only as root against the existing V1 staging vhost')
    raw = SOURCE.read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED:
        raise ValueError('V1 staging Nginx source changed; review before preparing a candidate')
    text = raw.decode()
    if text.count('proxy_pass http://yeodam_backend;') != 2 or text.count('proxy_pass http://yeodam_frontend;') != 1:
        raise ValueError('unexpected V1 staging proxy locations')
    candidate = text.replace('proxy_pass http://yeodam_backend;',
                             'proxy_pass http://127.0.0.1:19091;').replace(
                                 'proxy_pass http://yeodam_frontend;',
                                 'proxy_pass http://127.0.0.1:19091;')
    RUN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path, data in ((RUN_DIR / 'nginx.before.conf', raw),
                       (RUN_DIR / 'nginx.gate.candidate.conf', candidate.encode())):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    print('nginx_candidate_prepared; public route unchanged')


if __name__ == '__main__':
    main()
