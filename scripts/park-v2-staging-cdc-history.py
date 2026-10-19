#!/usr/bin/env python3
"""Stop completed B1 CDC cases without deleting their data or offsets.

Run on the Kafka host. By default, inspect only; --apply stops historical
sources, drains the ordered sinks, then stops those sinks. Outbox is untouched.
"""

import argparse
import json
import subprocess
import time
import urllib.request


BASE = 'http://127.0.0.1:8083'
BROKER = 'yeodam-v2-staging-kafka-kafka-1'
CASES = ('pilot304', 'full309', 'live313')
TABLES = ('users', 'trips', 'trip_regions')
SOURCES = tuple(f'yeodam-migration-b1-{case}-source' for case in CASES) + (
    'yeodam-migration-b1-live318-source',)
SINKS = tuple(f'yeodam-migration-b1-{case}-sink-ordered' for case in CASES)
TABLE_SINKS = tuple(f'yeodam-migration-b1-{case}-sink-{table}'
                    for case in CASES for table in TABLES)
FAILED_318_SINK = 'yeodam-migration-b1-live318-sink-users'


def api(path, method=None):
    request = urllib.request.Request(BASE + path, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        return json.loads(body) if body else None


def state(name):
    value = api('/connectors/' + name + '/status')
    return value['connector']['state'], tuple(task['state'] for task in value['tasks'])


def offset(name):
    result = subprocess.run([
        'docker', 'exec', BROKER, '/opt/kafka/bin/kafka-consumer-groups.sh',
        '--bootstrap-server', '127.0.0.1:9092', '--describe', '--group',
        'connect-' + name], check=True, capture_output=True, text=True)
    topic = name.removeprefix('yeodam-migration-b1-').removesuffix('-sink-ordered')
    expected = f'staging.yeodam.migration.b1.{topic}.ordered-records'
    rows = [line.split() for line in result.stdout.splitlines()
            if line.startswith('connect-' + name + ' ')]
    if len(rows) != 1 or rows[0][1:3] != [expected, '0']:
        raise RuntimeError(name + ': unexpected consumer group')
    current, end, lag = map(int, rows[0][3:6])
    if end - current != lag:
        raise RuntimeError(name + ': inconsistent lag fields')
    return current, end, lag


def wait_stopped(name):
    for _ in range(30):
        connector, tasks = state(name)
        if connector == 'STOPPED' and not tasks:
            return
        time.sleep(2)
    raise RuntimeError(name + ': did not reach STOPPED')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    for name in SOURCES + SINKS + TABLE_SINKS:
        connector, tasks = state(name)
        if connector not in ('RUNNING', 'PAUSED', 'STOPPED') or any(
                task not in ('RUNNING', 'PAUSED') for task in tasks):
            raise RuntimeError(name + ': unexpected state; inspect before parking')
        print(name, connector, ','.join(tasks), flush=True)
    failed_connector, failed_tasks = state(FAILED_318_SINK)
    if failed_connector not in ('RUNNING', 'STOPPED') or any(
            task not in ('FAILED', 'STOPPED') for task in failed_tasks):
        raise RuntimeError(FAILED_318_SINK + ': unexpected state')
    print(FAILED_318_SINK, failed_connector, ','.join(failed_tasks), flush=True)
    for name in SINKS:
        print(name, 'offset', *offset(name), flush=True)
    if not args.apply:
        return

    for name in SOURCES:
        if state(name)[0] != 'STOPPED':
            api('/connectors/' + name + '/stop', method='PUT')
        wait_stopped(name)
        print(name, 'stopped', flush=True)

    for name in SINKS:
        for _ in range(30):
            if offset(name)[2] == 0:
                break
            time.sleep(2)
        else:
            raise RuntimeError(name + ': did not drain after source pause')
        if state(name)[0] != 'STOPPED':
            api('/connectors/' + name + '/stop', method='PUT')
        wait_stopped(name)
        print(name, 'stopped-offset', *offset(name), flush=True)

    for name in TABLE_SINKS:
        if state(name)[0] != 'STOPPED':
            api('/connectors/' + name + '/stop', method='PUT')
        wait_stopped(name)
        print(name, 'stopped', flush=True)

    if state(FAILED_318_SINK)[0] != 'STOPPED':
        api('/connectors/' + FAILED_318_SINK + '/stop', method='PUT')
    wait_stopped(FAILED_318_SINK)
    print(FAILED_318_SINK, 'stopped-failed-task-preserved-in-evidence', flush=True)


if __name__ == '__main__':
    main()
