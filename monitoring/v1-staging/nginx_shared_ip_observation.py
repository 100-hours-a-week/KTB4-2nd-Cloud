#!/usr/bin/env python3
"""Temporary UUID-only GET correlation; preserves every rate/auth/routing directive."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from uuid import UUID

PREFIX = '''# BEGIN yeodam shared-ip456 observation
map "$request_method:$uri:$http_x_migration_run_id" $yeodam_trial_probe_id {
    default "";
    "~^GET:/api/trips:(?<yeodam_trial_probe_uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$" $yeodam_trial_probe_uuid;
}
# END yeodam shared-ip456 observation

'''
FIELD = '\n      \'"trial_probe_id":"$yeodam_trial_probe_id",\''
SITE = Path('/etc/nginx/sites-enabled/yeodam-v1-staging')


def instrument(text):
    if PREFIX in text and text.count(PREFIX) == text.count(FIELD) == 1:
        return text
    if 'yeodam_trial_probe' in text or 'trial_probe_id' in text:
        raise ValueError('Partial observer configuration')
    if text.count('log_format yeodam_json escape=json') != 1:
        raise ValueError('Expected escaping JSON log')
    matches = list(re.finditer(r'"request_id":"\$[A-Za-z0-9_]+",\'', text))
    if len(matches) != 1:
        raise ValueError('Expected one JSON request ID field')
    at = matches[0].end()
    return PREFIX + text[:at] + FIELD + text[at:]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reload_site(content):
    previous = SITE.read_bytes()
    try:
        SITE.write_bytes(content)
        subprocess.run(['nginx', '-t'], check=True, capture_output=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True, capture_output=True)
    except Exception:
        SITE.write_bytes(previous)
        subprocess.run(['nginx', '-t'], check=True, capture_output=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True, capture_output=True)
        raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['apply', 'rollback'])
    p.add_argument('--run-id', required=True)
    p.add_argument('--expected-site-sha256', required=True)
    a = p.parse_args()
    UUID(a.run_id)
    os.umask(0o077)
    if (os.geteuid() != 0 or Path('/var/lib/cloud/data/instance-id').read_text().strip()
            != 'i-05d320e3a87200d5a' or digest(SITE) != a.expected_site_sha256):
        raise ValueError('Exact App and unchanged config required')
    backup = Path('/var/backups/yeodam/nginx-shared-ip456') / a.run_id
    before = digest(SITE)
    if a.mode == 'apply':
        backup.mkdir(mode=0o700, parents=True, exist_ok=False)
        shutil.copy2(SITE, backup / 'site.conf')
        observed = instrument(SITE.read_text())
        reload_site(observed.encode())
        (backup / 'manifest.json').write_text(json.dumps({'originalSha': before, 'observedSha': digest(SITE)}))
    else:
        saved = json.loads((backup / 'manifest.json').read_text())
        if before != saved['observedSha'] or digest(backup / 'site.conf') != saved['originalSha']:
            raise ValueError('Other configuration edits; do not overwrite')
        reload_site((backup / 'site.conf').read_bytes())
    print(json.dumps({'mode': a.mode, 'beforeSha': before, 'afterSha': digest(SITE),
                      'defenseDirectivesChanged': False, 'backup': str(backup)}))


if __name__ == '__main__':
    main()
