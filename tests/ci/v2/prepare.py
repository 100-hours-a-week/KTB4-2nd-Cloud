"""Provision disposable CI topics/connector; no deployment endpoints accepted."""
import asyncio
import json
from pathlib import Path
import sys
import time

from aiokafka.admin import AIOKafkaAdminClient, NewTopic
import httpx


async def topics():
    names = []
    for flow in ['trip-create', 'trip-update', 'story-create']:
        for direction in ['request', 'result']:
            name = f'ci.yeodam.{flow}.{direction}'
            config = {'message.timestamp.type': 'LogAppendTime'} if direction == 'request' else {}
            names.extend([NewTopic(name, 2, 1, topic_configs=config), NewTopic(name + '-dlt', 2, 1)])
    for name in ['configs', 'offsets', 'status']:
        names.append(NewTopic('ci.connect-' + name, 1, 1, topic_configs={'cleanup.policy': 'compact'}))
    names.append(NewTopic('ci.yeodam.schema-history', 1, 1))
    names.append(NewTopic('__debezium-heartbeat.ci.yeodam.outbox', 1, 1))
    admin = AIOKafkaAdminClient(bootstrap_servers='kafka:9092')
    await admin.start()
    try:
        await admin.create_topics(names)
    finally:
        await admin.close()
    print('CI_TOPICS_CREATED', flush=True)


def connector():
    # Reuse staging's transformations; override only disposable connection/scope values.
    cfg = json.loads(Path('/ci-connector.json').read_text())
    cfg.update({
        'name': 'cloud-v2-ci', 'database.hostname': 'mysql',
        'database.user': 'ci_cdc', 'database.password': 'ci-cdc-only',
        'database.server.id': '2392',
        'heartbeat.interval.ms': '1000',
        'transforms.managedInsert.topic.regex': r'ci\.yeodam\.outbox\.yeodam_v2_ci\..*',
        'database.include.list': 'yeodam_v2_ci', 'topic.prefix': 'ci.yeodam.outbox',
        'schema.history.internal.kafka.bootstrap.servers': 'kafka:9092',
        'schema.history.internal.kafka.topic': 'ci.yeodam.schema-history',
    })
    cfg['table.include.list'] = cfg['table.include.list'].replace('yeodam_validation.', 'yeodam_v2_ci.')
    assert cfg['snapshot.mode'] == 'no_data'
    assert cfg['database.hostname'] == 'mysql'
    with httpx.Client(trust_env=False, timeout=20) as http:
        response = http.put('http://connect:8083/connectors/cloud-v2-ci/config', json=cfg)
        response.raise_for_status()
        for _ in range(90):
            response = http.get('http://connect:8083/connectors/cloud-v2-ci/status')
            if response.status_code == 200:
                state = response.json()
                if state['connector']['state'] == 'FAILED' or any(t['state'] == 'FAILED' for t in state['tasks']):
                    raise RuntimeError('CI connector failed: ' + json.dumps(state))
                if state['connector']['state'] == 'RUNNING' and state['tasks'] and all(t['state'] == 'RUNNING' for t in state['tasks']):
                    # RUNNING alone can precede the no_data snapshot's start position.
                    offsets = http.get('http://connect:8083/connectors/cloud-v2-ci/offsets')
                    if offsets.status_code == 200 and any(
                        item.get('offset', {}).get('file') and item['offset'].get('pos') is not None
                        for item in offsets.json().get('offsets', [])
                    ):
                        print('CI_CONNECTOR_RUNNING_WITH_COMMITTED_SOURCE_POSITION', flush=True)
                        return
            time.sleep(2)
    raise TimeoutError('CI connector readiness timed out')


if __name__ == '__main__':
    if sys.argv[1:] == ['topics']:
        asyncio.run(topics())
    elif sys.argv[1:] == ['connector']:
        connector()
    else:
        raise SystemExit('Expected topics or connector')
