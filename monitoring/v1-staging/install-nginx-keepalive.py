#!/usr/bin/env python3
"""Compare only V1 staging client idle timeouts; preserve routing and read policy."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from nginx_keepalive import instrument_keepalive

SITE = Path('/etc/nginx/sites-enabled/yeodam-v1-staging').resolve()
BACKUPS = Path('/var/backups/yeodam/nginx-keepalive')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def reload_nginx():
    subprocess.run(['nginx', '-t'], check=True)
    subprocess.run(['systemctl', 'reload', 'nginx'], check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['install', 'rollback'])
    p.add_argument('--expected-site-sha256', required=True)
    p.add_argument('--backup', type=Path)
    p.add_argument('--seconds', type=int, choices=[15,30,75])
    a = p.parse_args()
    if os.geteuid() != 0 or Path('/var/lib/cloud/data/instance-id').read_text().strip() != 'i-05d320e3a87200d5a':
        p.error('Root on the V1 staging App only')
    original = SITE.read_bytes()
    if digest(original) != a.expected_site_sha256:
        p.error('Site changed; inspect before applying or rolling back')
    if a.mode == 'rollback':
        if a.seconds is not None:
            p.error('--seconds is only for install')
        if not a.backup or a.backup.resolve().parent != BACKUPS:
            p.error('Use the original backup directory')
        backup = a.backup.resolve()
        meta = json.loads((backup/'manifest.json').read_text())
        restored = (backup/'site').read_bytes()
        if (meta['site'] != str(SITE) or digest(restored) != meta['beforeSha256']
                or digest(original) != meta['afterSha256']):
            p.error('Backup identity or checksum differs')
        try:
            SITE.write_bytes(restored)
            reload_nginx()
        except Exception:
            SITE.write_bytes(original)
            reload_nginx()
            raise
        print(json.dumps({'rolledBack': True, 'siteSha256': digest(restored)}))
        return
    if a.backup:
        p.error('--backup is only for rollback')
    if a.seconds is None:
        p.error('--seconds is required for install')
    updated = instrument_keepalive(original.decode(), a.seconds).encode()
    if updated == original:
        print(json.dumps({'changed': False, 'siteSha256': digest(original)}))
        return
    subprocess.run(['systemctl', 'is-active', '--quiet', 'nginx'], check=True)
    BACKUPS.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = Path(tempfile.mkdtemp(dir=BACKUPS, prefix='apply-'))
    (backup/'site').write_bytes(original)
    (backup/'site').chmod(0o600)
    meta = {'site': str(SITE), 'beforeSha256': digest(original), 'afterSha256': digest(updated), 'keepaliveSeconds': a.seconds}
    (backup/'manifest.json').write_text(json.dumps(meta))
    try:
        SITE.write_bytes(updated)
        reload_nginx()
    except Exception:
        SITE.write_bytes(original)
        reload_nginx()
        raise
    print(json.dumps({'changed': True, 'backup': str(backup), **meta}))


if __name__ == '__main__':
    main()
