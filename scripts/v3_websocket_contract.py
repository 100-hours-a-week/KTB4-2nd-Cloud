"""Opt-in WebSocket runtime checks; no cluster or credential access."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / 'deploy/v3/apps/files/websocket-contract.json').read_text())


def require(ok, message):
    if not ok:
        raise ValueError(message)


def prepare(config):
    require('SPRING_APPLICATION_JSON' not in config['configMapData'], 'Use structured WebSocket properties')
    require(config.get('springProperties') == CONTRACT['spring'], 'Use exact WebSocket Sentinel/TLS properties')
    config['configMapData']['SPRING_APPLICATION_JSON'] = json.dumps(CONTRACT['spring'], sort_keys=True, separators=(',', ':'))


def validate(configs):
    require(not configs or set(configs) == {'trip-websocket'}, 'Use a separate WebSocket release from BE')
    for c in configs.values():
        require(c['replicas'] == 1 and c['name'] == 'websocket', 'Initial WebSocket uses one Pod')
        require(not c['environment'] and not c['configMapRefs'], 'No unreviewed WebSocket envFrom or environment')
        require(c['ports'] == {'http': 8081} and c['service'] == {'enabled': True, 'port': 8081}, 'WebSocket uses HTTP8081 ClusterIP Service')
        data = c['configMapData']
        require(set(data) == set(CONTRACT['config']) | {'WEBSOCKET_ALLOWED_ORIGINS', 'RELEASE', 'SPRING_APPLICATION_JSON'}, 'Exact WebSocket config keys required')
        require(all(data.get(k) == v for k, v in CONTRACT['config'].items()), 'WebSocket peer, channel or runtime setting mismatch')
        require(re.fullmatch(r'https://[A-Za-z0-9.-]+(:[0-9]+)?', data['WEBSOCKET_ALLOWED_ORIGINS']), 'Explicit HTTPS origin required')
        require(re.fullmatch('[a-f0-9]{40}', data['RELEASE']) and re.fullmatch('[a-f0-9]{64}', c['configurationRevision']), 'Image source and public metadata revision required')
        require(c['secretRefs'] == CONTRACT['secrets'] and c['secretVolumes'] == CONTRACT['secretVolumes'], 'Use selected WebSocket credentials and public trust only')
        resources = json.loads(json.dumps(CONTRACT['resources']));resources['limits']['cpu'] = ''
        require(c['resources'] == resources, 'Keep minimum WebSocket resource budget')
        require(c['rollout'] == {'maxSurge': 0, 'maxUnavailable': 1}, 'Replace one WebSocket Pod without surge')
        require(c['terminationGracePeriodSeconds'] == 60 and c['preStopDelaySeconds'] == 5 and c['scratchSizeLimit'] == '64Mi', 'Keep WebSocket shutdown and scratch budget')
        for name in ('startup', 'liveness', 'readiness'):
            endpoint = 'readiness' if name == 'readiness' else 'liveness'
            require(c['probes'][name]['httpGet'] == {'path': '/api/actuator/health/' + endpoint, 'port': 'http', 'scheme': 'HTTP'}, 'Use WebSocket health endpoints')
