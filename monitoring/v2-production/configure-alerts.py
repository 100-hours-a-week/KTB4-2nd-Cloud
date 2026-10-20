#!/usr/bin/env python3
"""Reconcile only V2 production Grafana alerts. Secrets never leave this host's API request."""
import argparse
import base64
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

FOLDER = 'yeodam-v2-production-alerts'
RECEIVER = 'yeodam-v2-production-discord'
CONTACT_UID = 'v2-production-discord'
SCOPE = ['monitoring_scope', '=', 'v2-production']
TEST_UID = 'v2p-notification-verification'
BASE = '/api/v1/provisioning'


def build_rule(spec, paused=True):
    model = copy.deepcopy(spec.get('cloudwatch', {}))
    datasource = 'production-cloudwatch' if model else 'production-prometheus'
    if not model:
        model = {'expr': spec['expr'], 'instant': True, 'range': False}
    model.update(refId='A', intervalMs=1000, maxDataPoints=43200)
    return {
        'uid': spec['uid'], 'orgID': 1, 'folderUID': FOLDER,
        'ruleGroup': 'v2-production', 'title': spec['title'], 'condition': 'B',
        'for': spec['hold'], 'noDataState': spec.get('noDataState', 'KeepLast'),
        'execErrState': 'Error', 'isPaused': paused,
        'labels': {'env': 'production', 'monitoring_scope': 'v2-production',
                   'service': spec['service'], 'severity': spec['severity']},
        'annotations': {'summary': spec['summary'],
                        'description': '지표와 로그를 함께 확인하세요. 복구 통지는 조건 해제이며 사용자 작업 완료의 증거는 아닙니다.',
                        'dashboard': 'https://grafana.yeodam-2gether.com/d/' + spec['dashboard']},
        'data': [
            {'refId': 'A', 'queryType': '', 'relativeTimeRange': {'from': 600, 'to': 0},
             'datasourceUid': datasource, 'model': model},
            {'refId': 'B', 'queryType': '', 'relativeTimeRange': {'from': 0, 'to': 0},
             'datasourceUid': '-100', 'model': {'refId': 'B', 'type': 'classic_conditions',
                'conditions': [{'evaluator': {'type': spec.get('comparator', 'gt'),
                                             'params': [spec.get('threshold', 0)]},
                                'operator': {'type': 'and'}, 'query': {'params': ['A']},
                                'reducer': {'type': 'last', 'params': []}, 'type': 'query'}]}}
        ]}


def merge_policy(policy):
    """Preserve every unrelated route and the original default receiver."""
    result = copy.deepcopy(policy)
    routes = [r for r in result.get('routes', []) if SCOPE not in r.get('object_matchers', [])]
    routes.insert(0, {
        'receiver': RECEIVER, 'object_matchers': [SCOPE], 'continue': False,
        'group_by': ['grafana_folder', 'alertname'], 'group_wait': '30s',
        'group_interval': '5m', 'repeat_interval': '4h',
        'routes': [{'receiver': RECEIVER, 'object_matchers': [['verification', '=', 'true']],
                    'group_by': ['alertname'], 'group_wait': '5s',
                    'group_interval': '10s', 'repeat_interval': '4h'}]})
    result['routes'] = routes
    result.pop('provenance', None)
    return result


class Grafana:
    def __init__(self):
        password = Path('/etc/yeodam/plg/admin-password').read_text().strip()
        self.headers = {'Authorization': 'Basic ' + base64.b64encode(('admin:' + password).encode()).decode(),
                        'Content-Type': 'application/json'}

    def api(self, method, path, body=None, missing_ok=False):
        request = urllib.request.Request('http://127.0.0.1:3000' + path,
                  data=None if body is None else json.dumps(body).encode(), headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                content = response.read()
                return json.loads(content) if content else None
        except urllib.error.HTTPError as error:
            if missing_ok and error.code == 404: return None
            # Contact-point error bodies can contain request settings. Never print them.
            raise RuntimeError(f'Grafana {method} {path}: HTTP {error.code}') from None


def contact(grafana):
    output = subprocess.run(['aws', 'ssm', 'get-parameter', '--region', 'ap-northeast-2',
        '--name', '/yeodam/v1/observability/discord-webhook-url', '--with-decryption', '--output', 'json'],
        check=True, capture_output=True, text=True, timeout=30).stdout
    url = json.loads(output)['Parameter']['Value'].strip()
    parsed = urllib.parse.urlparse(url)
    if (parsed.scheme != 'https' or parsed.hostname not in {'discord.com', 'discordapp.com'}
            or not re.fullmatch(r'/api/webhooks/[0-9]+/[^/]+', parsed.path)
            or parsed.query or parsed.fragment or parsed.username):
        raise ValueError('Invalid Discord webhook parameter (value omitted)')
    body = {'uid': CONTACT_UID, 'name': RECEIVER, 'type': 'discord', 'disableResolveMessage': False,
            'settings': {'url': url, 'use_discord_username': True,
                'title': '[V2 운영] {{ if eq .Status "firing" }}장애{{ else }}복구 / 조건 해제{{ end }}',
                'message': '{{ len .Alerts }}건 / 아래 최대3건 표시\n{{ range $i, $alert := .Alerts }}{{ if lt $i 3 }}**{{ .Labels.alertname }}**\n{{ .Annotations.summary }}\n{{ .Annotations.description }}\n{{ .Annotations.dashboard }}\n{{ if .Annotations.grafana_state_reason }}상태 변경 이유: {{ .Annotations.grafana_state_reason }}\n{{ end }}{{ end }}{{ end }}'}}
    existing = grafana.api('GET', BASE + '/contact-points')
    found = next((c for c in existing if c['uid'] == CONTACT_UID), None)
    if found and (found['name'] != RECEIVER or found['type'] != 'discord'):
        raise ValueError('Managed contact UID belongs to a different contact')
    grafana.api('PUT' if found else 'POST', BASE + '/contact-points' + ('/' + CONTACT_UID if found else ''), body)


def check_queries(grafana, specs):
    results = []
    for spec in specs:
        if 'expr' in spec:
            url = 'http://127.0.0.1:9090/api/v1/query?' + urllib.parse.urlencode({'query': spec['expr']})
            response = json.load(urllib.request.urlopen(url, timeout=20))
            if response['status'] != 'success': raise RuntimeError('Prometheus query failed: ' + spec['uid'])
            values = [item['value'][1] for item in response['data']['result']]
        else:
            model = dict(spec['cloudwatch'], datasource={'uid': 'production-cloudwatch', 'type': 'cloudwatch'})
            data = grafana.api('POST', '/api/ds/query', {'from': str(int((time.time()-600)*1000)),
                       'to': str(int(time.time()*1000)), 'queries': [model]})['results']['A']
            if data.get('error'): raise RuntimeError('CloudWatch query failed: ' + spec['uid'])
            values = []
            for frame in data.get('frames', []):
                for field, column in zip(frame['schema']['fields'], frame['data']['values']):
                    if field['type'] == 'number':
                        valid = [v for v in column if v is not None]
                        if valid: values.append(str(valid[-1]))
        results.append({'uid': spec['uid'], 'values': values})
    print(json.dumps({'query_checks': results}))
    return results


def configure(grafana, specs, activate):
    # Parse every query before changing any alerting configuration.
    check_queries(grafana, specs)
    if not grafana.api('GET', '/api/folders/' + FOLDER, missing_ok=True):
        grafana.api('POST', '/api/folders', {'uid': FOLDER, 'title': 'V2 production alerts'})
    contact(grafana)
    policy = grafana.api('GET', BASE + '/policies')
    grafana.api('PUT', BASE + '/policies', merge_policy(policy))
    group = {'title': 'v2-production', 'folderUid': FOLDER, 'interval': 60,
             'rules': [build_rule(s, paused=not activate) for s in specs]}
    # Only this managed folder/group is replaced; no unrelated rules are deleted.
    grafana.api('PUT', BASE + '/folder/' + FOLDER + '/rule-groups/v2-production', group)
    print(json.dumps({'configured_rules': len(specs), 'active': activate}))


def delivery_counts():
    text = urllib.request.urlopen('http://127.0.0.1:3000/metrics', timeout=10).read().decode()
    attempts = failed = 0
    for line in text.splitlines():
        if line.startswith('#') or not re.search(r'integration="discord(?:\[[0-9]+\])?"', line): continue
        name = line.split('{', 1)[0]
        if name.endswith('_notifications_total'): attempts += float(line.rsplit(' ', 1)[1])
        if name.endswith('_notifications_failed_total'): failed += float(line.rsplit(' ', 1)[1])
    return attempts, failed


def write_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.next')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream: json.dump(state, stream)
    os.replace(temporary, path)


def verify(grafana, confirmed):
    if not confirmed: raise ValueError('Verification requires --confirm-two-messages')
    path = Path('/var/lib/yeodam/alert-verification/361.json')
    if path.exists(): raise ValueError('Verification already attempted. Inspect saved state; do not replay automatically.')
    before, failures = delivery_counts()
    state = {'phase': 'prepared', 'before': before, 'failed_before': failures}
    write_state(path, state)
    rule = build_rule({'uid': TEST_UID, 'title': '운영 모니터링 시험 장애 (실제 서비스 영향 없음)',
        'expr': 'vector(1)', 'hold': '0s', 'service': 'observability', 'severity': 'info',
        'summary': 'Cloud #361의 알림 전송 시험입니다. 실제 서비스 장애가 아닙니다.',
        'dashboard': 'yeodam-v2-services'}, paused=False)
    rule['ruleGroup'] = 'notification-verification'
    rule['labels']['verification'] = 'true'
    group_path = BASE + '/folder/' + FOLDER + '/rule-groups/notification-verification'
    def put(value):
        rule['data'][0]['model']['expr'] = 'vector(' + str(value) + ')'
        grafana.api('PUT', group_path, {'title': rule['ruleGroup'], 'folderUid': FOLDER,
                                       'interval': 10, 'rules': [rule]})
    def wait_for(count):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            current, failed = delivery_counts()
            if failed != failures: raise RuntimeError('Discord delivery failed; inspect notifier without replaying test')
            if current >= count: return current
            time.sleep(3)
        raise RuntimeError('Notification verification timeout; inspect saved state before any retry')
    state['phase'] = 'firing_requested'; write_state(path, state)
    put(1)
    first = wait_for(before + 1)
    state.update(phase='resolve_requested', after_firing=first); write_state(path, state)
    put(0)
    second = wait_for(before + 2)
    state.update(phase='complete', after_resolved=second, failed_after=delivery_counts()[1]); write_state(path, state)
    grafana.api('DELETE', BASE + '/alert-rules/' + TEST_UID)
    print(json.dumps(state))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['check', 'install', 'activate', 'pause', 'verify'], required=True)
    parser.add_argument('--config', type=Path, default=Path('/opt/yeodam/plg/alerts.json'))
    parser.add_argument('--confirm-two-messages', action='store_true')
    args = parser.parse_args()
    specs = json.loads(args.config.read_text())
    grafana = Grafana()
    if args.mode == 'check': check_queries(grafana, specs)
    elif args.mode in {'install', 'activate'}: configure(grafana, specs, args.mode == 'activate')
    elif args.mode == 'verify': verify(grafana, args.confirm_two_messages)
    else:
        group = grafana.api('GET', BASE + '/folder/' + FOLDER + '/rule-groups/v2-production')
        for rule in group['rules']: rule['isPaused'] = True
        grafana.api('PUT', BASE + '/folder/' + FOLDER + '/rule-groups/v2-production', group)
        print('Paused managed V2 production rules only')
