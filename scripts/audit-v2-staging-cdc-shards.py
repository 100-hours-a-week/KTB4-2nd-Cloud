#!/usr/bin/env python3
"""Audit the fixed #336 trip shards without printing CDC row contents."""

import json
import subprocess
import sys


TOPIC = 'staging.yeodam.migration.b1.live336.ordered-records'
BROKER = 'yeodam-v2-staging-kafka-kafka-1'
PARTITIONS = 8


def kafka_tool(name, *args):
    return ['docker', 'exec', '-i', BROKER, '/opt/kafka/bin/' + name,
            '--bootstrap-server', '127.0.0.1:9092', *args]


def offsets(moment):
    output = subprocess.run(kafka_tool('kafka-get-offsets.sh', '--topic', TOPIC,
                                       '--time', moment), check=True, capture_output=True,
                            text=True).stdout
    result = {}
    for line in output.splitlines():
        topic, partition, value = line.split(':')
        if topic != TOPIC:
            raise ValueError('unexpected CDC topic')
        result[int(partition)] = int(value)
    if set(result) != set(range(PARTITIONS)):
        raise ValueError('unexpected CDC partition count')
    return result


def audit_partition(lines, partition, expected):
    if partition not in range(PARTITIONS) or expected < 0:
        raise ValueError('invalid audit boundary')
    first = {}
    user_events = 0
    counts = {'trips': 0, 'trip_regions': 0}
    index = 0
    for line in lines:
        index += 1
        try:
            event = json.loads(line)
            payload = event['payload']
            table = payload['source']['table']
            operation = payload['op']
            if table == 'users':
                user_events += 1
                continue
            if table not in counts or operation not in ('c', 'u', 'd'):
                raise ValueError('unexpected table or operation')
            row = payload['after'] if operation != 'd' else payload['before']
            trip_id = row['trip_id']
            if not isinstance(trip_id, int):
                raise ValueError('missing numeric trip_id')
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError('invalid CDC event; preserve topic') from exc
        counts[table] += 1
        first.setdefault((table, trip_id), index)
    if index != expected:
        raise ValueError('CDC partition event count mismatch')
    reversed_pairs = sum(1 for (table, trip_id), at in first.items()
                         if table == 'trip_regions' and ('trips', trip_id) in first
                         and first[('trips', trip_id)] > at)
    return {'partition': partition, 'events': index, 'users': user_events,
            'counts': counts, 'first': first, 'reversedPairs': reversed_pairs}


def summarize(results):
    owners = {}
    cross_partition = 0
    for result in results:
        for _, trip_id in result['first']:
            previous = owners.setdefault(trip_id, result['partition'])
            if previous != result['partition']:
                cross_partition += 1
    summary = {'events': sum(row['events'] for row in results),
               'usersEvents': sum(row['users'] for row in results),
               'tripsEvents': sum(row['counts']['trips'] for row in results),
               'regionsEvents': sum(row['counts']['trip_regions'] for row in results),
               'crossPartitionTripIds': cross_partition,
               'reversedParentChildPairs': sum(row['reversedPairs'] for row in results),
               'partitionEvents': [row['events'] for row in results]}
    summary['passed'] = (summary['events'] > 0 and summary['usersEvents'] == 0 and
                         cross_partition == 0 and summary['reversedParentChildPairs'] == 0)
    return summary


def audit():
    begins, ends = offsets('-2'), offsets('-1')
    if any(begins[partition] != 0 for partition in range(PARTITIONS)):
        raise ValueError('CDC topic was truncated; original offsets must be preserved')
    results = []
    for partition in range(PARTITIONS):
        count = ends[partition]
        if count == 0:
            results.append(audit_partition([], partition, 0))
            continue
        command = kafka_tool('kafka-console-consumer.sh', '--topic', TOPIC,
                             '--partition', str(partition), '--offset', 'earliest',
                             '--max-messages', str(count), '--timeout-ms', '120000')
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True) as consumer:
            try:
                result = audit_partition(consumer.stdout, partition, count)
            except ValueError:
                consumer.terminate()
                raise
            if consumer.wait() != 0:
                raise ValueError('Kafka audit consumer failed')
            results.append(result)
    return summarize(results)


if __name__ == '__main__':
    try:
        result = audit()
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(0 if result['passed'] else 1)
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print('CDC_SHARD_AUDIT_FAILED', type(error).__name__, str(error)[:120], file=sys.stderr)
        raise SystemExit(2)
