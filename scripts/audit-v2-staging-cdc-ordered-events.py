#!/usr/bin/env python3
"""Summarize a bounded ordered-topic JSON stream without printing row contents."""

import argparse
import json
import subprocess
import sys


TABLES = ('users', 'trips', 'trip_regions')
OPS = ('r', 'c', 'u', 'd')
TOPIC = 'staging.yeodam.migration.b1.live323.ordered-records'
BROKER_CONTAINER = 'yeodam-v2-staging-kafka-kafka-1'


def kafka_tool(name, *arguments):
    return ['docker', 'exec', '-i', BROKER_CONTAINER,
            '/opt/kafka/bin/' + name, '--bootstrap-server', '127.0.0.1:9092',
            *arguments]


def topic_offset(moment):
    result = subprocess.run(kafka_tool('kafka-get-offsets.sh', '--topic', TOPIC,
                                      '--time', moment), capture_output=True,
                            text=True, check=True)
    fields = result.stdout.strip().split(':')
    if len(fields) != 3 or fields[:2] != [TOPIC, '0']:
        raise ValueError('ordered topic is not one expected partition')
    return int(fields[2])


def coordinate(position):
    filename, offset = position
    prefix, separator, suffix = filename.rpartition('.')
    if not separator or not suffix.isdecimal():
        raise ValueError('unexpected MySQL binlog filename')
    return prefix, int(suffix), offset


def audit(lines, expected_count):
    counts = {table: {op: 0 for op in OPS} for table in TABLES}
    violations = []
    total = 0
    last_snapshot_table = -1
    snapshot_position = None
    first_stream_position = None
    last_stream_position = None
    streaming_started = False

    def violation(kind, index):
        if len(violations) < 10:
            violations.append({'kind': kind, 'index': index})

    for line in lines:
        total += 1
        try:
            event = json.loads(line)
            payload = event['payload']
            source = payload['source']
            table = source['table']
            op = payload['op']
            position = [source['file'], source['pos']]
            if (table not in TABLES or op not in OPS or
                    not isinstance(position[0], str) or not isinstance(position[1], int)):
                raise ValueError('unexpected CDC event')
            coordinate(position)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f'invalid event at index {total}; preserve topic for inspection') from exc

        counts[table][op] += 1
        if op == 'r':
            if streaming_started:
                violation('snapshot_after_stream', total)
            table_index = TABLES.index(table)
            if table_index < last_snapshot_table:
                violation('snapshot_fk_order', total)
            last_snapshot_table = max(last_snapshot_table, table_index)
            if snapshot_position is None:
                snapshot_position = position
            elif position != snapshot_position:
                violation('snapshot_position_changed', total)
        else:
            streaming_started = True
            if first_stream_position is None:
                first_stream_position = position
                if snapshot_position is not None:
                    snapshot_file, snapshot_number, snapshot_offset = coordinate(snapshot_position)
                    stream_file, stream_number, stream_offset = coordinate(position)
                    if (snapshot_file != stream_file or
                            (stream_number, stream_offset) < (snapshot_number, snapshot_offset)):
                        violation('stream_before_snapshot_position', total)
            last_stream_position = position

    if total != expected_count:
        violation('event_count_mismatch', total)
    if snapshot_position is None:
        violation('snapshot_absent', total)
    if first_stream_position is None:
        violation('stream_absent', total)
    return {
        'events': total,
        'expectedEvents': expected_count,
        'counts': counts,
        'snapshotPosition': snapshot_position,
        'firstStreamPosition': first_stream_position,
        'lastStreamPosition': last_stream_position,
        'violations': violations,
        'passed': not violations,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-count', type=int,
                        help='parse JSONL from stdin; omit to read the fixed Kafka topic')
    args = parser.parse_args()
    if args.expected_count is not None:
        if args.expected_count < 1:
            parser.error('expected-count must be positive')
        result = audit(sys.stdin, args.expected_count)
    else:
        if topic_offset('-2') != 0:
            raise ValueError('ordered topic earliest offset is not zero')
        end = topic_offset('-1')
        if end < 1:
            raise ValueError('ordered topic has no events')
        command = kafka_tool('kafka-console-consumer.sh', '--topic', TOPIC,
                             '--from-beginning', '--max-messages', str(end),
                             '--timeout-ms', '600000')
        with subprocess.Popen(command, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True) as consumer:
            try:
                result = audit(consumer.stdout, end)
            except ValueError:
                consumer.terminate()
                raise
            if consumer.wait() != 0:
                raise ValueError('Kafka consumer failed; preserve topic and offsets')
    print(json.dumps(result, sort_keys=True))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(2)
