#!/usr/bin/env python3
"""Limit only the completed B1 experiments' ordered JDBC Sink pools.

Run on the Kafka/Connect host. The default mode only checks the live config.
No connector, topic, consumer offset, or destination row is deleted.
"""

import argparse
import json
import time
import urllib.request


BASE = 'http://127.0.0.1:8083'
TARGETS = {
    'pilot304': 'yeodam_cdc',
    'full309': 'yeodam_cdc_full309',
    'live313': 'yeodam_cdc_live313',
}
POOL = {
    'connection.pool.min_size': '1',
    'connection.pool.max_size': '2',
    'connection.pool.acquire_increment': '1',
}


def api(path, *, value=None, method=None):
    body = None if value is None else json.dumps(value).encode()
    request = urllib.request.Request(BASE + path, data=body, method=method,
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def is_running(name):
    status = api('/connectors/' + name + '/status')
    return status['connector']['state'] == 'RUNNING' and all(
        task['state'] == 'RUNNING' for task in status['tasks']) and len(status['tasks']) == 1


def expected(case, schema):
    name = f'yeodam-migration-b1-{case}-sink-ordered'
    topic = f'staging.yeodam.migration.b1.{case}.ordered-records'
    url = ('jdbc:mysql://mysql-cdc-test.staging.yeodam.internal:3306/'
           f'{schema}?sslMode=REQUIRED')
    return name, topic, url


def inspect(name, topic, url):
    config = api('/connectors/' + name + '/config')
    if (config.get('connector.class') != 'io.debezium.connector.jdbc.JdbcSinkConnector'
            or config.get('connection.url') != url
            or config.get('connection.username') != 'yeodam_cdc_sink'
            or config.get('topics') != topic
            or config.get('tasks.max') != '1'
            or not is_running(name)):
        raise RuntimeError(f'{name}: unexpected identity or task state')
    for key, target in POOL.items():
        if config.get(key) not in (None, target):
            raise RuntimeError(f'{name}: existing {key} differs; inspect before changing')
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()

    planned = []
    for case, schema in TARGETS.items():
        name, topic, url = expected(case, schema)
        config = inspect(name, topic, url)
        planned.append((name, topic, url, config))
        print(name, 'bounded' if all(config.get(k) == v for k, v in POOL.items())
              else 'default-pool', flush=True)

    if not args.apply:
        return

    for name, topic, url, config in planned:
        if all(config.get(k) == v for k, v in POOL.items()):
            continue
        updated = {**config, **POOL}
        # Never print the REST response: config includes a password provider reference.
        api('/connectors/' + name + '/config', value=updated, method='PUT')
        for _ in range(30):
            time.sleep(2)
            if is_running(name):
                break
        else:
            raise RuntimeError(f'{name}: task did not return to RUNNING; stop here')
        inspect(name, topic, url)
        if not all(api('/connectors/' + name + '/config').get(k) == v
                   for k, v in POOL.items()):
            raise RuntimeError(f'{name}: pool setting was not retained; stop here')
        print(name, 'bounded-running', flush=True)


if __name__ == '__main__':
    main()
