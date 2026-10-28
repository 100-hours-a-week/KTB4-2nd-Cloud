#!/usr/bin/env python3
"""Preserve a completed/aborted clone case and restore its immutable baseline."""
import argparse
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from uuid import UUID

BUCKET = 'yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2'
HOSTS = {'source': ('i-03efe8652117623c2', 'vol-0c5214fdd56505b1a', '/var/lib/mysql'),
         'target': ('i-0c9aee73a0dc9b59d', 'vol-0ab5d56c53b887f0e', '/srv/yeodam/mysql')}
BASELINES = {
    '30': ('3502031b-0040-4333-9a8c-9ce63d9a2ce8', 'ab1a0935f6335f0385f5df2cfb2eb4c75c9fce4f05a742d9e81bed3bfe2cfae6'),
    '50': ('56f1a134-7867-42d6-84df-1c19f030916e', '51474607af2aae8212b32bf4093fcde164dfcbda8f01345e88e8eac595559cb3')}


def module(name, path):
    s = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    return m


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--role', choices=HOSTS, required=True)
    p.add_argument('--baseline', choices=BASELINES, required=True)
    p.add_argument('--attempt', required=True)
    a = p.parse_args(); UUID(a.attempt); os.umask(0o077)
    m = module('clone444', '/var/lib/yeodam/migration444/prepare.py')
    r = module('reset391', '/run/yeodam-b391/b-integration376-reset.py')
    b = module('baseline408', '/run/yeodam-b391/b-integration408-baseline.py')
    instance, volume, mount = HOSTS[a.role]
    subprocess.run(['mountpoint', '-q', mount], check=True)
    device = m.run(['findmnt', '-n', '-o', 'SOURCE', mount])
    serial = m.run(['lsblk', '-dn', '-o', 'SERIAL', device])
    m.guard(a.role, instance, volume, m.identity(), json.loads((m.ROOT / 'identity.json').read_text()), serial)
    b.protected(r, a.role)
    if r.query(a.role, "SELECT COUNT(*) FROM information_schema.processlist WHERE USER NOT IN ('root','event_scheduler','system user') AND ID!=CONNECTION_ID();") != '0':
        raise RuntimeError('Stop clone DB clients before restoration')
    directory = Path('/var/lib/yeodam/migration455/restores') / a.attempt
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    origin, expected = BASELINES[a.baseline]
    archive = directory / 'baseline.sql.gz'
    r.transfer('s3://' + BUCKET + '/migration/rehearsal-reset/' + origin + '/b391-target.sql.gz', str(archive))
    if r.sha(archive) != expected:
        raise RuntimeError('Historical archive SHA differs; no schema replacement')
    before = directory / 'before.sql.gz'; b.preserve(r, a.role, before)
    prefix = 'migration/performance455/baseline-restores/' + a.attempt + '/' + a.role
    r.transfer(str(before), 's3://' + BUCKET + '/' + prefix + '-before.sql.gz')
    preserved = {'key': prefix + '-before.sql.gz', 'sha256': r.sha(before), 'bytes': before.stat().st_size}
    (directory / 'preserved.json').write_text(json.dumps(preserved))
    r.transfer(str(directory / 'preserved.json'), 's3://' + BUCKET + '/' + prefix + '-before.ready')
    b.protected(r, a.role)
    r.query(a.role, 'SET PERSIST super_read_only=OFF; SET PERSIST read_only=OFF;')
    try:
        with gzip.open(archive, 'rb') as content, (directory / 'import.stderr.private').open('wb') as errors:
            process = subprocess.Popen(r.command(a.role) + ['--default-character-set=utf8mb4'], stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL, stderr=errors)
            try:
                process.stdin.write(('SET SESSION sql_log_bin=0; DROP DATABASE `' + r.SCHEMA + '`; CREATE DATABASE `' + r.SCHEMA + '` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci; USE `' + r.SCHEMA + '`;\n').encode())
                for block in iter(lambda: content.read(65536), b''): process.stdin.write(block)
                process.stdin.close()
                if process.wait(): raise RuntimeError('Import failed; private stderr preserved')
            except BaseException:
                if process.poll() is None: process.kill(); process.wait()
                raise
    finally:
        r.query(a.role, 'SET PERSIST read_only=ON; SET PERSIST super_read_only=ON;')
    fingerprint = b.fingerprint(r, a.role)
    regions = int(r.query(a.role, 'SELECT COUNT(*) FROM ' + r.SCHEMA + '.trip_regions;'))
    if a.baseline == '30' and (fingerprint['trips'], regions, fingerprint['journal'], len(fingerprint['tables'])) != (2021037, 2021037, 219511, 33):
        raise RuntimeError('Historical 30 RPS baseline counts differ')
    if a.baseline == '50' and (fingerprint['trips'] != 2048038 or len(fingerprint['tables']) != 33):
        raise RuntimeError('Historical 50 RPS baseline counts differ')
    value = {'instanceId': instance, 'role': a.role, 'attempt': a.attempt,
             'baselineSha256': expected, 'preserved': preserved, 'fingerprint': fingerprint,
             'regions': regions, 'gtidReset': False, 'binlogImportEnabled': False}
    receipt = directory / 'restored.json'; receipt.write_text(json.dumps(value, indent=2))
    latest = m.ROOT / 'baseline-restored.json'
    if latest.exists(): (directory / 'previous-baseline-receipt.json').write_bytes(latest.read_bytes())
    latest.write_text(json.dumps(value, indent=2))
    print(json.dumps(value))


if __name__ == '__main__': main()
