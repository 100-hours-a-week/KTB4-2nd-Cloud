#!/usr/bin/env python3
"""Apply only Grafana web settings; keep Prometheus/Loki and alert conditions intact."""
import argparse
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request

HOSTS = {
    'staging': ('i-05c76ccb2b53f9279', 'grafana-staging.yeodam-2gether.com'),
    'production': ('i-015e8dd17ade9e192', 'grafana.yeodam-2gether.com'),
}
ROOT = Path('/opt/yeodam/plg')


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE)


def api(path, password, body=None):
    headers = {'Authorization': 'Basic ' + base64.b64encode(('admin:' + password).encode()).decode(),
               'Content-Type': 'application/json'}
    req = urllib.request.Request('http://127.0.0.1:3000' + path, headers=headers,
          data=None if body is None else json.dumps(body).encode(), method='GET' if body is None else 'PUT')
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.load(response)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--environment', choices=HOSTS, required=True)
    p.add_argument('--compose-source', type=Path, required=True)
    args = p.parse_args()
    expected_id, domain = HOSTS[args.environment]
    if os.geteuid() != 0 or Path('/var/lib/cloud/data/instance-id').read_text().strip() != expected_id:
        raise ValueError('Run as root on the selected PLG instance')
    password = Path('/etc/yeodam/plg/admin-password').read_text().strip()
    if len(password) < 32:
        raise ValueError('Existing administrator password is too short for web access')
    source = args.compose_source.resolve()
    source_config = json.loads(run('docker', 'compose', '-f', str(source), 'config', '--format', 'json'))
    current = ROOT / 'plg-compose.yml'
    old_config = json.loads(run('docker', 'compose', '-f', str(current), 'config', '--format', 'json'))
    new_grafana = source_config['services']['grafana']
    if new_grafana['environment'].get('GF_SERVER_ROOT_URL') != 'https://' + domain + '/':
        raise ValueError('Unexpected public URL')
    if source_config['name'] != old_config['name']:
        raise ValueError('Compose project mismatch')
    for service in ('prometheus', 'loki'):
        # Source-relative bind paths differ; compare only non-volume fields.
        before = {k: v for k, v in old_config['services'][service].items() if k != 'volumes'}
        after = {k: v for k, v in source_config['services'][service].items() if k != 'volumes'}
        if before != after:
            raise ValueError('Refusing unrelated collector change: ' + service)
    if new_grafana['image'] != old_config['services']['grafana']['image']:
        raise ValueError('This operation does not upgrade Grafana')
    rule_prefix = 'v2s-' if args.environment == 'staging' else 'v2p-'
    before_rules = api('/api/v1/provisioning/alert-rules', password)
    backup = Path('/var/lib/yeodam/grafana-web') / str(time.time_ns())
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(current, backup / 'plg-compose.yml')
    (backup / 'alert-rules.json').write_text(json.dumps(before_rules))
    (backup / 'alert-rules.json').chmod(0o600)
    try:
        if source.read_bytes() != current.read_bytes():
            shutil.copyfile(source, current)
            run('docker', 'compose', '-f', str(current), 'up', '-d', '--no-deps', '--pull', 'never', 'grafana')
        for attempt in range(30):
            try:
                if api('/api/health', password)['database'] == 'ok':
                    break
            except Exception:
                time.sleep(2)
        else:
            raise RuntimeError('Grafana did not become healthy')
    except Exception:
        shutil.copyfile(backup / 'plg-compose.yml', current)
        run('docker', 'compose', '-f', str(current), 'up', '-d', '--no-deps', '--pull', 'never', 'grafana')
        raise RuntimeError('Grafana configuration failed; previous Compose restored') from None
    updated = 0
    for rule in before_rules:
        if not rule['uid'].startswith(rule_prefix):
            continue
        dashboard = rule.get('annotations', {}).get('dashboard', '')
        if '/d/' not in dashboard:
            continue
        rule['annotations']['dashboard'] = 'https://' + domain + '/d/' + dashboard.split('/d/', 1)[1]
        # Preserve expressions, pending period, labels and paused state.
        body = {k: v for k, v in rule.items() if k not in ('id', 'updated', 'provenance')}
        api('/api/v1/provisioning/alert-rules/' + rule['uid'], password, body)
        updated += 1
    print(json.dumps({'environment': args.environment, 'url': 'https://' + domain,
                      'updated_dashboard_links': updated, 'backup': str(backup)}))


if __name__ == '__main__':
    main()
