#!/usr/bin/env python3
"""Adapt the cloned Target backup destination without changing dump behavior."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

TARGET = 'i-0c9aee73a0dc9b59d'
VOLUME = 'vol-0ab5d56c53b887f0e'
PREFIX = 'migration/performance455/full/'


def render_backup(text):
    for name, suffix in [('key', '.sql.gz'), ('ready_key', '.ready')]:
        old = name + '="full/${stamp}' + suffix + '"'
        new = name + '="' + PREFIX + '${stamp}' + suffix + '"'
        if text.count(new) == 1 and old not in text:
            continue
        if text.count(old) != 1:
            raise ValueError('Unexpected backup script; no replacement')
        text = text.replace(old, new, 1)
    return text


def main():
    spec = importlib.util.spec_from_file_location('clone444', '/var/lib/yeodam/migration444/prepare.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    mount = '/srv/yeodam/mysql'
    subprocess.run(['mountpoint', '-q', mount], check=True)
    device = m.run(['findmnt', '-n', '-o', 'SOURCE', mount])
    serial = m.run(['lsblk', '-dn', '-o', 'SERIAL', device])
    m.guard('target', TARGET, VOLUME, m.identity(),
            json.loads((m.ROOT / 'identity.json').read_text()), serial)
    path = Path('/usr/local/sbin/yeodam-mysql-full-backup')
    before = path.read_text(); after = render_backup(before)
    subprocess.run(['bash', '-n'], input=after, text=True, check=True)
    directory = Path('/var/lib/yeodam/migration455'); directory.mkdir(mode=0o700, exist_ok=True)
    original = directory / 'full-backup.before.sh'
    if not original.exists():
        original.write_text(before); original.chmod(0o700)
    if after != before:
        staged = path.with_suffix('.455-next'); staged.write_text(after); staged.chmod(0o700); staged.replace(path)
    value = {'instanceId': TARGET, 'dataVolumeId': VOLUME, 'prefix': PREFIX,
             'beforeSha256': hashlib.sha256(before.encode()).hexdigest(),
             'afterSha256': hashlib.sha256(after.encode()).hexdigest(), 'dumpOptionsChanged': False}
    (directory / 'backup-prefix-ready.json').write_text(json.dumps(value))
    print(json.dumps(value))


if __name__ == '__main__':
    main()
