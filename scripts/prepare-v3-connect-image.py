#!/usr/bin/env python3
"""Prepare a minimal, checksum-verified Connect image context; no AWS or credentials."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'deploy/v3/connect-image'
LOCK = json.loads((SOURCE / 'source.lock.json').read_text())


def prepare(directory):
    directory.mkdir(parents=True, exist_ok=True)
    downloads = directory / 'downloads';downloads.mkdir(exist_ok=True)
    plugins = directory / 'plugins';plugins.mkdir(exist_ok=True)
    if plugins.is_symlink() or downloads.is_symlink():
        raise ValueError('Generated context directories must not be symlinks')
    expected=set()
    for artifact in LOCK['artifacts']:
        url = artifact['url'];name = artifact['filename']
        if not url.startswith('https://repo.maven.apache.org/maven2/') or name != Path(url).name:
            raise ValueError('Only the reviewed official Maven artifacts are allowed')
        file = downloads / name
        if file.is_symlink():raise ValueError("Unsafe cached artifact symlink")
        if not file.exists() or hashlib.sha256(file.read_bytes()).hexdigest() != artifact['sha256']:
            data = urllib.request.urlopen(url, timeout=60).read()
            if len(data) != artifact['bytes'] or hashlib.sha256(data).hexdigest() != artifact['sha256']:
                raise ValueError('Public artifact checksum/size mismatch')
            file.write_bytes(data)
        if name.endswith('.tar.gz'):
            with tarfile.open(file) as archive:
                for member in archive.getmembers():
                    path = PurePosixPath(member.name)
                    if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                        raise ValueError('Unsafe plugin archive member')
                    if member.isfile() and (path.suffix == '.jar' or path.name.upper().startswith(('LICENSE', 'NOTICE', 'COPYRIGHT'))):
                        expected.add(path.name)
                        target = plugins / path.name
                        if target.is_symlink():raise ValueError('Unsafe existing plugin symlink')
                        if target.exists() and target.read_bytes() != archive.extractfile(member).read():
                            raise ValueError('Conflicting plugin archive entry')
                        target.write_bytes(archive.extractfile(member).read());target.chmod(0o644)
        else:
            expected.add(name)
            if (plugins / name).is_symlink():raise ValueError('Unsafe existing plugin symlink')
            shutil.copyfile(file, plugins / name)
    if {f.name for f in plugins.iterdir()} != expected:
        raise ValueError('Stale/unreviewed plugin files; use a fresh generated context')
    shutil.copyfile(SOURCE / 'Dockerfile', directory / 'Dockerfile')
    # Only these inputs enter Docker; cached archives and logs are excluded.
    (directory / '.dockerignore').write_text('*\n!Dockerfile\n!plugins/\n!plugins/**\n')
    print('Verified Connect image context prepared; credentials and runtime endpoints absent.')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',type=Path,required=True)
    prepare(p.parse_args().directory)
