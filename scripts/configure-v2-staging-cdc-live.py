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
CASE = os.environ.get('CDC_LIVE_CASE', '313')
ORIGINAL_CASES = ('318', '32210', '32230', '322100')
FIXED_CASES = ('323',)
BULK_CASES = ('331', '331r', '331s')
REPLAY_BATCH_SIZES = {'324replay': '500', '324replay1000': '1000'}
REPLAY_CASES = tuple(REPLAY_BATCH_SIZES)
REPLAY_TOPIC = 'staging.yeodam.migration.b1.live323.ordered-records'
REPLAY_SOURCE = 'yeodam-migration-b1-live323-source'
REPLAY_PREVIOUS_SINK = 'yeodam-migration-b1-live323-sink-ordered'
if CASE not in ('313', *ORIGINAL_CASES, *FIXED_CASES, *BULK_CASES, *REPLAY_CASES):
    raise RuntimeError('unsupported CDC live case')
PREFIX = f'staging.yeodam.migration.b1.live{CASE}'
SOURCE = f'yeodam-migration-b1-live{CASE}-source'
BROKER = 'yeodam-v2-staging-kafka-kafka-1'
TABLES = ('users', 'trips', 'trip_regions')
ORDERED_TOPIC = PREFIX + '.ordered-records'
FULL_SINK_PREFIX = f'yeodam-migration-b1-live{CASE}-sink-'


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


def topic_end(name):
    output = subprocess.run(['docker', 'exec', BROKER,
                             '/opt/kafka/bin/kafka-get-offsets.sh', '--bootstrap-server',
                             '127.0.0.1:9092', '--topic', name, '--time', '-1'],
                            check=True, capture_output=True, text=True).stdout.strip()
    fields = output.split(':')
    if len(fields) != 3 or fields[:2] != [name, '0']:
        raise RuntimeError('unexpected topic offset format: ' + name)
    return int(fields[2])


def sink_lag(name, topic):
    group = 'connect-' + name
    output = subprocess.run(['docker', 'exec', BROKER,
                             '/opt/kafka/bin/kafka-consumer-groups.sh',
                             '--bootstrap-server', '127.0.0.1:9092',
                             '--describe', '--group', group],
                            check=True, capture_output=True, text=True).stdout
    rows = [line.split() for line in output.splitlines() if line.startswith(group + ' ')]
    if len(rows) != 1 or rows[0][1:3] != [topic, '0']:
        raise RuntimeError('unexpected consumer group state: ' + group)
    try:
        current, end, lag = map(int, rows[0][3:6])
    except ValueError as exc:
        raise RuntimeError('consumer group has no committed offset yet: ' + group) from exc
    # The producer keeps writing during this run. A second end-offset query can
    # already be ahead of the consumer-group snapshot, so compare one snapshot.
    if end - current != lag:
        raise RuntimeError('consumer lag fields disagree: ' + group)
    return current, end, lag


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
                    raise RuntimeError('existing full schema-history policy is unexpected')


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


def configure_ordered_route(data):
    data['transforms'] = 'route'
    data['transforms.route.type'] = 'io.debezium.transforms.ByLogicalTableRouter'
    data['transforms.route.topic.regex'] = (
        rf'^{re.escape(PREFIX)}\.yeodam_v1_source\.'
        r'(users|trips|trip_regions)$')
    data['transforms.route.topic.replacement'] = ORDERED_TOPIC
    data['transforms.route.key.enforce.uniqueness'] = 'false'
    data['producer.override.acks'] = 'all'
    data['producer.override.enable.idempotence'] = 'true'
    data['producer.override.max.in.flight.requests.per.connection'] = '1'


def existing_credential(kind):
    directory = Path('/etc/yeodam/kafka/cdc')
    if not directory.is_dir():
        raise RuntimeError('existing Connect credential mount is missing')
    path = directory / f'b1-{kind}.properties'
    stat = path.stat()
    if stat.st_uid != 1001 or stat.st_gid != 1001 or stat.st_mode & 0o077:
        raise RuntimeError('existing Connect credential ownership/mode is unexpected')


def bulk_import_receipt():
    run_id = os.environ.get('CDC_BULK_RUN_ID', '')
    if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', run_id):
        raise RuntimeError('CDC_BULK_RUN_ID is required for one-use bulk Sink')
    base = f'migration/cdc-live331/{run_id}'
    files = []
    for name in ('snapshot.ready.json', 'snapshot.imported.json'):
        path = Path('/run') / f'cdc-live331-{run_id}-{name}'
        subprocess.run(['aws', 's3', 'cp',
                        f's3://yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2/{base}/{name}',
                        str(path), '--region', 'ap-northeast-2', '--only-show-errors'],
                       check=True, capture_output=True, text=True)
        files.append(path)
    try:
        ready, imported = (json.loads(path.read_text()) for path in files)
    finally:
        for path in files:
            path.unlink(missing_ok=True)
    if (ready.get('runId') != run_id or imported.get('runId') != run_id or
            ready.get('archiveSha256') != imported.get('archiveSha256') or
            imported.get('destinationSchema') != 'yeodam_cdc_live' + CASE or
            any(imported.get('foreignKeyOrphans', {}).values()) or
            not all(imported.get('rows', {}).get(table, 0) > 0 for table in TABLES)):
        raise RuntimeError('B1 import receipt is incomplete or inconsistent')
    print('BULK_IMPORT_RECEIPT', run_id, flush=True)


def config(kind):
    key = 'CDC_LIVE_SOURCE_B64' if kind == 'source' else 'CDC_LIVE_SINK_B64'
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(f'{key} is missing')
    return json.loads(base64.b64decode(value))


def register(kind):
    if kind == 'replay-sink':
        if CASE not in REPLAY_CASES:
            raise RuntimeError('replay-sink requires the isolated replay case')
        existing_credential('sink')
        names = set(api('/connectors'))
        if FULL_SINK_PREFIX + 'ordered' in names:
            raise RuntimeError('replay sink already exists; inspect its offset before retry')
        for preserved in (REPLAY_SOURCE, REPLAY_PREVIOUS_SINK):
            if api('/connectors/' + preserved + '/status')['connector']['state'] != 'STOPPED':
                raise RuntimeError('original pilot connector must remain stopped: ' + preserved)
        if CASE == '324replay1000' and api(
                '/connectors/yeodam-migration-b1-live324replay-sink-ordered/status'
        )['connector']['state'] != 'STOPPED':
            raise RuntimeError('previous replay sink must remain stopped')
        outbox = api('/connectors/yeodam-staging-outbox/status')
        if outbox['connector']['state'] != 'RUNNING' or any(
                task['state'] != 'RUNNING' for task in outbox['tasks']):
            raise RuntimeError('service Outbox must remain running')
        if REPLAY_TOPIC not in set(kafka('--list').splitlines()) or topic_end(REPLAY_TOPIC) == 0:
            raise RuntimeError('preserved ordered topic is missing or empty')
        name = FULL_SINK_PREFIX + 'ordered'
        data = config('sink')
        data.update({'name': name, 'topics': REPLAY_TOPIC,
                     'batch.size': REPLAY_BATCH_SIZES[CASE],
                     'consumer.override.max.poll.records': REPLAY_BATCH_SIZES[CASE],
                     'use.reduction.buffer': 'false'})
        plugin = 'io.debezium.connector.jdbc.JdbcSinkConnector'
        validate = api(f'/connector-plugins/{plugin}/config/validate', data, 'PUT')
        if validate['error_count']:
            fields = [item['definition']['name'] for item in validate.get('configs', [])
                      if item.get('value', {}).get('errors')]
            raise RuntimeError('invalid replay sink fields: ' + ','.join(fields))
        api('/connectors/' + name + '/config', data, 'PUT')
        print('REGISTERED', name, flush=True)
        return
    expected = set(topic_names())
    if kind.startswith('ordered-'):
        expected.add(ORDERED_TOPIC)
    if not expected.issubset(set(kafka('--list').splitlines())):
        raise RuntimeError('create the four isolated topics first')
    source_kind = kind in ('initial-source', 'ordered-source')
    existing_credential('source' if source_kind else 'sink')
    data = config('source' if source_kind else 'sink')
    if source_kind:
        name = SOURCE
        assert data['name'] == name and data['topic.prefix'] == PREFIX
        plugin = 'io.debezium.connector.mysql.MySqlConnector'
        if kind == 'initial-source' and name in api('/connectors'):
            raise RuntimeError('full source already exists; inspect its offset before retry')
        if kind == 'initial-source':
            if any(topic_end(topic) != 0 for topic in topic_names()):
                raise RuntimeError('full topics already have records; inspect before creating source')
            if CASE in (*FIXED_CASES, *BULK_CASES):
                if (ORDERED_TOPIC not in set(kafka('--list').splitlines()) or
                        topic_end(ORDERED_TOPIC) != 0):
                    raise RuntimeError('fixed ordered topic must exist and be empty before source')
                configure_ordered_route(data)
                if CASE in BULK_CASES:
                    data['snapshot.mode'] = 'no_data'
                    # A fresh logical name/empty topic is mandatory. A restart
                    # must recover its own offset, never start from current tail.
                    if data.get('database.server.id') != {'331': '5412', '331r': '5413',
                                                          '331s': '5414'}[CASE]:
                        raise RuntimeError('unexpected bulk CDC server ID')
        if kind == 'ordered-source':
            if name not in api('/connectors'):
                raise RuntimeError('initial source is missing')
            state = api('/connectors/' + name + '/status')
            if state['connector']['state'] != 'PAUSED':
                raise RuntimeError('pause the full source before changing topic routing')
            if any(api('/connectors/' + FULL_SINK_PREFIX + table + '/status')
                   ['connector']['state'] != 'PAUSED' for table in TABLES):
                raise RuntimeError('drain and pause all three snapshot sinks first')
            # This full topic is append-only; table-specific Struct keys stay intact.
            configure_ordered_route(data)
    else:
        if kind == 'ordered-sink':
            if CASE not in (*FIXED_CASES, *BULK_CASES):
                for table in TABLES:
                    old = FULL_SINK_PREFIX + table
                    if api('/connectors/' + old + '/status')['connector']['state'] != 'PAUSED':
                        raise RuntimeError('pause every table-specific sink before ordered-sink')
            source_config = api('/connectors/' + SOURCE + '/config')
            if (source_config.get('transforms') != 'route' or
                    source_config.get('transforms.route.topic.replacement') != ORDERED_TOPIC):
                raise RuntimeError('source is not routed to the ordered topic')
            if CASE in BULK_CASES:
                bulk_ready()
            name = FULL_SINK_PREFIX + 'ordered'
            if name in api('/connectors'):
                raise RuntimeError('ordered sink already exists; inspect offset before retry')
            data['topics'] = ORDERED_TOPIC
            # live331s isolates the FK failure observed with 500-record
            # cross-table batches. This proves order, not 100 RPS capacity.
            bulk_batch = '1' if CASE == '331s' else '500'
            data['batch.size'] = bulk_batch if CASE in BULK_CASES else '1'
            data['consumer.override.max.poll.records'] = bulk_batch if CASE in BULK_CASES else '1'
            if CASE in BULK_CASES:
                bulk_import_receipt()
                data['use.reduction.buffer'] = 'false'
                data['consumer.override.auto.offset.reset'] = 'earliest'
                # Import must have completed before the first delta write.
                if topic_end(ORDERED_TOPIC) == 0:
                    raise RuntimeError('no committed delta event before bulk replay')
        else:
            table = kind.removeprefix('sink-')
            assert kind.startswith('sink-') and table in TABLES
            name = FULL_SINK_PREFIX + table
            if name in api('/connectors'):
                raise RuntimeError('sink already exists; inspect offset before retry')
            if api('/connectors/' + SOURCE + '/config').get('transforms') == 'route':
                raise RuntimeError('full source already routes incremental events')
            source_offsets = api('/connectors/' + SOURCE + '/offsets').get('offsets', [])
            if len(source_offsets) != 1 or source_offsets[0].get('offset', {}).get('snapshot') is not None:
                raise RuntimeError('full source snapshot has not completed')
            if table != 'users':
                parent = TABLES[TABLES.index(table) - 1]
                parent_name = FULL_SINK_PREFIX + parent
                parent_status = api('/connectors/' + parent_name + '/status')
                if (parent_status['connector']['state'] != 'RUNNING' or
                        any(task['state'] != 'RUNNING' for task in parent_status['tasks']) or
                        sink_lag(parent_name, f'{PREFIX}.yeodam_v1_source.{parent}')[2] != 0):
                    raise RuntimeError('parent snapshot sink has not drained: ' + parent)
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
    if CASE in REPLAY_CASES:
        names = set(api('/connectors'))
        replay_names = ([REPLAY_SOURCE, REPLAY_PREVIOUS_SINK]
                        + (['yeodam-migration-b1-live324replay-sink-ordered']
                           if CASE == '324replay1000' else [])
                        + [FULL_SINK_PREFIX + 'ordered', 'yeodam-staging-outbox'])
        for name in replay_names:
            if name not in names:
                raise RuntimeError('expected connector is missing: ' + name)
            for attempt in range(10):
                try:
                    value = api('/connectors/' + name + '/status')
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code != 404 or attempt == 9:
                        raise
                    time.sleep(1)
            print(name, value['connector']['state'],
                  ','.join(task['state'] for task in value['tasks']), flush=True)
        print('TOPIC_END', REPLAY_TOPIC, topic_end(REPLAY_TOPIC), flush=True)
        try:
            print('SINK_OFFSET', FULL_SINK_PREFIX + 'ordered',
                  *sink_lag(FULL_SINK_PREFIX + 'ordered', REPLAY_TOPIC), flush=True)
        except RuntimeError as exc:
            if not str(exc).startswith('consumer group has no committed offset yet:'):
                raise
            print('SINK_OFFSET', FULL_SINK_PREFIX + 'ordered', 'UNCOMMITTED', flush=True)
        return
    names = api('/connectors')
    for name in [SOURCE] + [FULL_SINK_PREFIX + table for table in TABLES] + [
            FULL_SINK_PREFIX + 'ordered']:
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
    for preserved in ('yeodam-migration-b1-pilot304-source',
                      'yeodam-migration-b1-pilot304-sink-ordered',
                      'yeodam-migration-b1-full309-source',
                      'yeodam-migration-b1-full309-sink-ordered'):
        value = api('/connectors/' + preserved + '/status')
        print(preserved, value['connector']['state'],
              ','.join(t['state'] for t in value['tasks']), flush=True)
    topics = set(kafka('--list').splitlines())
    for topic in (*topic_names(), ORDERED_TOPIC):
        if topic in topics:
            print('TOPIC_END', topic, topic_end(topic), flush=True)
    for table in (*TABLES, 'ordered'):
        name = FULL_SINK_PREFIX + table
        if name in names:
            topic = ORDERED_TOPIC if table == 'ordered' else f'{PREFIX}.yeodam_v1_source.{table}'
            try:
                current, end, lag = sink_lag(name, topic)
                print('SINK_OFFSET', name, current, end, lag, flush=True)
            except RuntimeError as exc:
                if not str(exc).startswith('consumer group has no committed offset yet:'):
                    raise
                print('SINK_OFFSET', name, 'UNCOMMITTED', flush=True)
    if SOURCE in names:
        source_config = api('/connectors/' + SOURCE + '/config')
        print('SOURCE_ROUTE', source_config.get('transforms', 'NONE'),
              source_config.get('transforms.route.topic.replacement', 'NONE'), flush=True)
        try:
            offsets = api('/connectors/' + SOURCE + '/offsets').get('offsets', [])
            for row in offsets:
                point = row.get('offset', {})
                print('SOURCE_OFFSET', point.get('file'), point.get('pos'),
                      point.get('snapshot'), flush=True)
        except urllib.error.HTTPError:
            print('SOURCE_OFFSET UNAVAILABLE', flush=True)


def bulk_ready():
    if CASE not in BULK_CASES:
        raise RuntimeError('bulk-ready is only for the separated path')
    config = api('/connectors/' + SOURCE + '/config')
    state = api('/connectors/' + SOURCE + '/status')
    if (config.get('snapshot.mode') != 'no_data' or
            config.get('transforms.route.topic.replacement') != ORDERED_TOPIC or
            state['connector']['state'] != 'RUNNING' or
            not state['tasks'] or any(t['state'] != 'RUNNING' for t in state['tasks'])):
        raise RuntimeError('no_data Source is not streaming to ordered Topic')
    offsets = api('/connectors/' + SOURCE + '/offsets').get('offsets', [])
    if len(offsets) != 1 or offsets[0].get('offset', {}).get('snapshot') is not None:
        raise RuntimeError('Source Binlog offset is not established')
    count = topic_end(ORDERED_TOPIC)
    if count < 1:
        raise RuntimeError('no API event observed before the bulk dump')
    print('BULK_READY', ORDERED_TOPIC, 'topicEnd', count,
          'binlogFile', offsets[0]['offset'].get('file'),
          'binlogPos', offsets[0]['offset'].get('pos'), flush=True)


def pause_legacy_sinks():
    if api('/connectors/' + SOURCE + '/status')['connector']['state'] != 'PAUSED':
        raise RuntimeError('pause source and drain its final table-topic offsets first')
    names = set(api('/connectors'))
    for table in TABLES:
        name = FULL_SINK_PREFIX + table
        if name not in names:
            raise RuntimeError('full sink missing: ' + name)
        topic = f'{PREFIX}.yeodam_v1_source.{table}'
        if sink_lag(name, topic)[2] != 0:
            raise RuntimeError('full sink still has records: ' + name)
    for table in TABLES:
        name = FULL_SINK_PREFIX + table
        if name not in names:
            raise RuntimeError('legacy full sink missing: ' + name)
        request = urllib.request.Request(BASE + '/connectors/' + name + '/pause',
                                         method='PUT')
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 202:
                raise RuntimeError('pause rejected: ' + name)
        print('PAUSED_LEGACY_SINK', name, flush=True)


def change_state(name, action):
    if name not in api('/connectors'):
        raise RuntimeError('connector missing: ' + name)
    request = urllib.request.Request(BASE + '/connectors/' + name + '/' + action,
                                     method='PUT')
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 202:
            raise RuntimeError('connector state change rejected: ' + name)
    print(action.upper(), name, flush=True)


def recover_legacy(table, action):
    if api('/connectors/' + SOURCE + '/status')['connector']['state'] != 'PAUSED':
        raise RuntimeError('source must remain paused while recovering boundary records')
    ordered = FULL_SINK_PREFIX + 'ordered'
    if (api('/connectors/' + ordered + '/status')['connector']['state'] != 'RUNNING' or
            sink_lag(ordered, ORDERED_TOPIC)[2] != 0):
        raise RuntimeError('ordered sink must finish before boundary recovery')
    name = FULL_SINK_PREFIX + table
    current_state = api('/connectors/' + name + '/status')['connector']['state']
    topic = f'{PREFIX}.yeodam_v1_source.{table}'
    lag = sink_lag(name, topic)[2]
    if action == 'resume':
        if current_state != 'PAUSED' or lag == 0:
            raise RuntimeError('expected paused legacy sink with pending records')
        if table == 'trip_regions':
            parent = FULL_SINK_PREFIX + 'trips'
            if (api('/connectors/' + parent + '/status')['connector']['state'] != 'PAUSED' or
                    sink_lag(parent, f'{PREFIX}.yeodam_v1_source.trips')[2] != 0):
                raise RuntimeError('recover all parent trips before regions')
    elif current_state != 'RUNNING' or lag != 0:
        raise RuntimeError('legacy sink must drain before pausing again')
    change_state(name, action)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('topics', 'initial-source', 'sink-users',
                                         'sink-trips', 'sink-trip_regions', 'ordered-topic',
                                         'pause-legacy-sinks', 'ordered-source',
                                         'ordered-sink', 'replay-sink', 'bulk-ready',
                                         'pause-source', 'resume-source',
                                         'resume-legacy-trips', 'pause-legacy-trips',
                                         'resume-legacy-trip_regions',
                                         'pause-legacy-trip_regions',
                                         'status'))
    phase = parser.parse_args().phase
    if CASE in ORIGINAL_CASES and phase not in ('topics', 'initial-source', 'sink-users',
                                                'sink-trips', 'sink-trip_regions', 'status'):
        raise RuntimeError('original case keeps table topics and does not pause or recover')
    if CASE in FIXED_CASES and phase not in ('topics', 'initial-source', 'ordered-sink', 'status'):
        raise RuntimeError('fixed case routes snapshot and binlog to one topic from the start')
    if CASE in BULK_CASES and phase not in ('topics', 'initial-source', 'bulk-ready',
                                           'ordered-sink', 'status'):
        raise RuntimeError('bulk case only permits no_data source and post-import ordered sink')
    if CASE in REPLAY_CASES and phase not in ('replay-sink', 'status'):
        raise RuntimeError('replay case only consumes the preserved ordered topic')
    if os.geteuid() != 0:
        raise RuntimeError('run as root on Kafka host')
    if phase == 'topics':
        ensure_topics()
        if CASE in (*FIXED_CASES, *BULK_CASES):
            ensure_ordered_topic()
    elif phase == 'ordered-topic':
        ensure_ordered_topic()
    elif phase == 'pause-legacy-sinks':
        pause_legacy_sinks()
        status()
    elif phase == 'pause-source':
        change_state(SOURCE, 'pause')
        status()
    elif phase == 'resume-source':
        if api('/connectors/' + FULL_SINK_PREFIX + 'ordered/status')\
                ['connector']['state'] != 'RUNNING':
            raise RuntimeError('ordered sink must be running before source resumes')
        change_state(SOURCE, 'resume')
        status()
    elif phase == 'status':
        status()
    elif phase == 'bulk-ready':
        bulk_ready()
    elif phase.startswith(('resume-legacy-', 'pause-legacy-')):
        action, _, table = phase.split('-', 2)
        table = table.removeprefix('legacy-')
        recover_legacy(table, action)
        status()
    else:
        register(phase)
        status()


if __name__ == '__main__':
    try:
        main()
    except (AssertionError, ValueError, KeyError, OSError, urllib.error.HTTPError,
            subprocess.CalledProcessError, RuntimeError) as exc:
        print('B1_FULL_FAILED', type(exc).__name__, str(exc)[:200], flush=True)
        raise SystemExit(1)
