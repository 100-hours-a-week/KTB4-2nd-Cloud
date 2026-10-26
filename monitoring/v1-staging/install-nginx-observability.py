#!/usr/bin/env python3
"""Staging App only. Back up and patch current files; never render over routing."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request
from nginx_observability import instrument_site, instrument_alloy

ROOT = Path(__file__).resolve().parent
SITE = Path('/etc/nginx/sites-enabled/yeodam-v1-staging').resolve()
STATUS = Path('/etc/nginx/conf.d/yeodam-v1-status.conf')
ALLOY = Path('/opt/yeodam/v1-source-observability/v1-source.alloy')
BACKUPS = Path('/var/backups/yeodam/nginx-observability')
COMPOSE = ['docker', 'compose', '-f', str(ROOT/'nginx-exporter-compose.yml')]


def run(args):
    # Keep full diagnostics in the SSM command result; never print file contents.
    subprocess.run(args, check=True)


def reload_alloy():
    with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:12345/-/reload', method='POST', data=b''), timeout=10) as r:
        if r.status != 200:
            raise RuntimeError('Alloy reload failed')


def restore(backup):
    for row in json.loads((backup/'manifest.json').read_text()):
        path = Path(row['path'])
        if row['exists']:
            # In-place write retains the inode of the Docker bind-mounted Alloy file.
            path.write_bytes((backup/row['name']).read_bytes())
            os.chmod(path, row['mode'])
        elif path.exists():
            path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['install', 'rollback'])
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--expected-site-sha256')
    args = parser.parse_args()
    if os.geteuid() != 0 or Path('/var/lib/cloud/data/instance-id').read_text().strip() != 'i-05d320e3a87200d5a':
        raise SystemExit('Run as root on the V1 staging App only')
    if args.mode == 'rollback':
        if not args.backup or args.backup.resolve().parent != BACKUPS:
            raise SystemExit('Use an original backup directory')
        restore(args.backup.resolve())
        run(['nginx', '-t']); run(['systemctl', 'reload', 'nginx']); reload_alloy()
        run(COMPOSE+['down'])
        print('Restored configuration; logs, Alloy positions and app containers preserved')
        return
    if args.backup:
        parser.error('--backup is for rollback only')
    if not args.expected_site_sha256 or hashlib.sha256(SITE.read_bytes()).hexdigest() != args.expected_site_sha256:
        raise SystemExit('Site hash changed or missing; inspect current configuration first')
    site = instrument_site(SITE.read_text())
    alloy = instrument_alloy(ALLOY.read_text())
    status = (ROOT/'nginx-status.conf').read_text()
    run(['systemctl', 'is-active', '--quiet', 'nginx'])
    run(COMPOSE+['config', '--quiet'])
    # Fail early on image availability, before changing Nginx or Alloy.
    run(COMPOSE+['pull'])
    BACKUPS.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = Path(tempfile.mkdtemp(dir=BACKUPS, prefix='apply-'))
    rows = []
    for name, path in [('site', SITE), ('alloy', ALLOY), ('status', STATUS)]:
        row = {'name':name, 'path':str(path), 'exists':path.exists(), 'mode':path.stat().st_mode & 0o777 if path.exists() else 0o644}
        if path.exists():
            shutil.copyfile(path, backup/name); os.chmod(backup/name, 0o600)
        rows.append(row)
    (backup/'manifest.json').write_text(json.dumps(rows))
    print('BACKUP', backup, flush=True)
    try:
        SITE.write_text(site); ALLOY.write_text(alloy); STATUS.write_text(status); STATUS.chmod(0o644)
        # Validate the running Alloy version before asking it to reload.
        run(['docker', 'exec', 'yeodam-v1-source-observability-alloy-1', '/bin/alloy', 'validate', '/etc/alloy/config.alloy'])
        run(['nginx', '-t']); run(['systemctl', 'reload', 'nginx'])
        run(COMPOSE+['up', '-d'])
        reload_alloy()
    except Exception:
        restore(backup)
        run(['nginx', '-t']); run(['systemctl', 'reload', 'nginx']); reload_alloy()
        # The new Compose project is separate from app and existing Alloy projects.
        run(COMPOSE+['down'])
        raise
    print('Installed; verify metrics/logs. Rollback: python3', ROOT/'install-nginx-observability.py', 'rollback --backup', backup)


if __name__ == '__main__':
    main()
