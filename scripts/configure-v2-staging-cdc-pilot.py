#!/usr/bin/env python3
"""Run on the Kafka host. This never removes topics, offsets, or other connectors."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.request

BASE = 'http://127.0.0.1:8083'
PREFIX = 'staging.yeodam.migration.b1.pilot304'
SOURCE = 'yeodam-migration-b1-pilot304-source'
BROKER = 'yeodam-v2-staging-kafka-kafka-1'
TABLES = ('users', 'trips', 'trip_regions')
ORDERED_TOPIC = PREFIX + '.ordered-records'
PARAMETERS = {
    'source': '/yeodam/v2/staging/cdc-test/SOURCE_PASSWORD',
    'sink': '/yeodam/v2/staging/cdc-test/SINK_PASSWORD',
}


def api(path, value=None, method=None):
    data = None if value is None else json.dumps(value).encode()
    request = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def kafka(*args):
    return subprocess.run(['docker', 'exec', BROKER,
                           '/opt/kafka/bin/kafka-topics.sh', '--bootstrap-server',
                           '127.0.0.1:9092', *args], check=True,
                          capture_output=True, text=True).stdout


def kafka_config(*args):
    return subprocess.run(['docker', 'exec', BROKER,
                           '/opt/kafka/bin/kafka-configs.sh', '--bootstrap-server',
                           '127.0.0.1:9092', *args], check=True,
                          capture_output=True, text=True).stdout


def topic_names():
    return tuple(f'{PREFIX}.yeodam_v1_source.{name}' for name in TABLES) + (f'{PREFIX}.schema-history',)


def ensure_topics():
    existing = set(kafka('--list').splitlines())
    for name in topic_names():
        if name not in existing:
            options = ['--create', '--topic', name, '--partitions', '1',
                       '--replication-factor', '1']
            if name.endswith('.schema-history'):
                # Debezium 3.4 writes keyless schema-history records; compaction rejects them.
                options += ['--config', 'cleanup.policy=delete',
                            '--config', 'retention.ms=-1']
            else:
                options += ['--config', 'retention.ms=604800000']
            kafka(*options)
            print('CREATED_TOPIC', name, flush=True)
        else:
            description = kafka('--describe', '--topic', name)
            if 'PartitionCount: 1' not in description or 'ReplicationFactor: 1' not in description:
                raise RuntimeError(f'unexpected shape for {name}')
            print('EXISTING_TOPIC', name, flush=True)
            if name.endswith('.schema-history'):
                config = kafka_config('--entity-type', 'topics', '--entity-name', name,
                                      '--describe', '--all')
                if 'cleanup.policy=compact' in config or 'retention.ms=-1' not in config:
                    kafka_config('--entity-type', 'topics', '--entity-name', name,
                                 '--alter', '--add-config',
                                 'cleanup.policy=delete,retention.ms=-1')
                    print('CORRECTED_PILOT_SCHEMA_HISTORY_POLICY', name, flush=True)


def ensure_ordered_topic():
    if ORDERED_TOPIC not in set(kafka('--list').splitlines()):
        kafka('--create', '--topic', ORDERED_TOPIC, '--partitions', '1',
              '--replication-factor', '1', '--config', 'cleanup.policy=delete',
              '--config', 'retention.ms=604800000')
        print('CREATED_TOPIC', ORDERED_TOPIC, flush=True)
    else:
        description = kafka('--describe', '--topic', ORDERED_TOPIC)
        if 'PartitionCount: 1' not in description or 'ReplicationFactor: 1' not in description:
            raise RuntimeError('unexpected ordered topic shape')
        print('EXISTING_TOPIC', ORDERED_TOPIC, flush=True)


def credential(kind):
    result = subprocess.run(['aws', 'ssm', 'get-parameter', '--region', 'ap-northeast-2',
                             '--name', PARAMETERS[kind], '--with-decryption'],
                            capture_output=True, text=True, check=True)
    password = json.loads(result.stdout)['Parameter']['Value']
    if not re.fullmatch(r'[0-9a-f]{32}', password):
        raise RuntimeError('unexpected credential format')
    directory = Path('/etc/yeodam/kafka/cdc')
    if not directory.is_dir():
        raise RuntimeError('existing Connect credential mount is missing')
    path = directory / f'b1-{kind}.properties'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o400)
    with os.fdopen(fd, 'w') as stream:
        stream.write('password=' + password + '\n')
    os.chown(path, 1001, 1001)
    path.chmod(0o400)


def config(kind):
    key = 'CDC_PILOT_SOURCE_B64' if kind == 'source' else 'CDC_PILOT_SINK_B64'
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(f'{key} is missing')
    return json.loads(base64.b64decode(value))


def register(kind):
    expected = set(topic_names())
    if kind.startswith('ordered-'):
        expected.add(ORDERED_TOPIC)
    if not expected.issubset(set(kafka('--list').splitlines())):
        raise RuntimeError('create the four isolated topics first')
    source_kind = kind in ('initial-source', 'ordered-source')
    credential('source' if source_kind else 'sink')
    data = config('source' if source_kind else 'sink')
    if source_kind:
        name = SOURCE
        assert data['name'] == name and data['topic.prefix'] == PREFIX
        plugin = 'io.debezium.connector.mysql.MySqlConnector'
        if kind == 'initial-source' and name in api('/connectors'):
            current = api('/connectors/' + name + '/config')
            if current.get('transforms') == 'route':
                raise RuntimeError('ordered source is active; refusing to restore table-specific routing')
        if kind == 'ordered-source':
            data['transforms'] = 'route'
            data['transforms.route.type'] = 'io.debezium.transforms.ByLogicalTableRouter'
            data['transforms.route.topic.regex'] = (
                r'^staging\.yeodam\.migration\.b1\.pilot304\.yeodam_v1_source\.'
                r'(users|trips|trip_regions)$')
            data['transforms.route.topic.replacement'] = ORDERED_TOPIC
            # This pilot topic is append-only; table-specific Struct keys stay intact.
            data['transforms.route.key.enforce.uniqueness'] = 'false'
            data['producer.override.acks'] = 'all'
            data['producer.override.enable.idempotence'] = 'true'
            data['producer.override.max.in.flight.requests.per.connection'] = '1'
    else:
        if kind == 'ordered-sink':
            for table in TABLES:
                old = f'yeodam-migration-b1-pilot304-sink-{table}'
                if api('/connectors/' + old + '/status')['connector']['state'] != 'PAUSED':
                    raise RuntimeError('pause every table-specific sink before ordered-sink')
            name = 'yeodam-migration-b1-pilot304-sink-ordered'
            data['topics'] = ORDERED_TOPIC
            data['batch.size'] = '1'
            data['consumer.override.max.poll.records'] = '1'
        else:
            table = kind.removeprefix('sink-')
            assert kind.startswith('sink-') and table in TABLES
            name = f'yeodam-migration-b1-pilot304-sink-{table}'
            data['topics'] = f'{PREFIX}.yeodam_v1_source.{table}'
            data['primary.key.fields'] = {'users':'user_id','trips':'trip_id',
                                          'trip_regions':'region_id'}[table]
        plugin = 'io.debezium.connector.jdbc.JdbcSinkConnector'
    data['name'] = name
    validate = api(f'/connector-plugins/{plugin}/config/validate', data, 'PUT')
    if validate['error_count']:
        fields = [item['definition']['name'] for item in validate.get('configs', [])
                  if item.get('value', {}).get('errors')]
        raise RuntimeError('invalid connector fields: ' + ','.join(fields))
    api('/connectors/' + name + '/config', data, 'PUT')
    print('REGISTERED', name, flush=True)


def status():
    names = api('/connectors')
    for name in [SOURCE] + [f'yeodam-migration-b1-pilot304-sink-{table}' for table in TABLES] + [
            'yeodam-migration-b1-pilot304-sink-ordered']:
        if name in names:
            for attempt in range(10):
                try:
                    value = api('/connectors/' + name + '/status')
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code != 404 or attempt == 9:
                        raise
                    time.sleep(1)
            print(name, value['connector']['state'],
                  ','.join(t['state'] for t in value['tasks']), flush=True)
        else:
            print(name, 'ABSENT', flush=True)
    outbox = api('/connectors/yeodam-staging-outbox/status')
    print('yeodam-staging-outbox', outbox['connector']['state'],
          ','.join(t['state'] for t in outbox['tasks']), flush=True)


def pause_legacy_sinks():
    names = set(api('/connectors'))
    for table in TABLES:
        name = f'yeodam-migration-b1-pilot304-sink-{table}'
        if name not in names:
            raise RuntimeError('legacy pilot sink missing: ' + name)
        request = urllib.request.Request(BASE + '/connectors/' + name + '/pause',
                                         method='PUT')
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 202:
                raise RuntimeError('pause rejected: ' + name)
        print('PAUSED_LEGACY_SINK', name, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('topics', 'initial-source', 'sink-users',
                                         'sink-trips', 'sink-trip_regions', 'ordered-topic',
                                         'pause-legacy-sinks', 'ordered-source',
                                         'ordered-sink', 'status'))
    phase = parser.parse_args().phase
    if os.geteuid() != 0:
        raise RuntimeError('run as root on Kafka host')
    if phase == 'topics':
        ensure_topics()
    elif phase == 'ordered-topic':
        ensure_ordered_topic()
    elif phase == 'pause-legacy-sinks':
        pause_legacy_sinks()
        status()
    elif phase == 'status':
        status()
    else:
        register(phase)
        status()


if __name__ == '__main__':
    try:
        main()
    except (AssertionError, ValueError, KeyError, OSError, urllib.error.HTTPError,
            subprocess.CalledProcessError, RuntimeError) as exc:
        print('B1_PILOT_FAILED', type(exc).__name__, str(exc)[:200], flush=True)
        raise SystemExit(1)
