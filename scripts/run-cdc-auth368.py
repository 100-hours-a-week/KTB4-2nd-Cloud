#!/usr/bin/env python3
"""Apply one protected authentication CDC topic to an isolated Redis prefix."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdc_auth368 import apply_change, decrypt_event


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, help='private A rehearsal controller JSON')
    parser.add_argument('--topic', required=True)
    parser.add_argument('--group', required=True)
    parser.add_argument('--key-file', required=True)
    parser.add_argument('--bootstrap', required=True)
    parser.add_argument('--max-events', type=int, default=0)
    args = parser.parse_args()

    if not args.topic.startswith('staging.yeodam.migration.auth368.'):
        raise ValueError('refuse a non-pilot authentication topic')
    if not args.group.startswith('yeodam-auth368-') or args.max_events < 0:
        raise ValueError('refuse an unscoped group or negative bound')
    key_path = Path(args.key_file)
    if key_path.stat().st_mode & 0o077:
        raise ValueError('auth key file must be private')
    key = bytes.fromhex(key_path.read_text().strip())
    config = json.loads(Path(args.config).read_text())
    prefix = config['redis']['prefix']
    if prefix != 'yeodam:cutover:' + config['run_id'] + ':':
        raise ValueError('Redis prefix does not match isolated Run')
    if config['source_timezone'] not in ('Asia/Seoul', 'UTC'):
        raise ValueError('unknown Source timezone')

    from confluent_kafka import Consumer, KafkaException
    import redis

    section = config['redis']
    client = redis.Redis(host=section['host'], port=section.get('port', 6379),
                         db=section.get('db', 0), password=os.environ[section['password_env']],
                         decode_responses=True, socket_connect_timeout=3, socket_timeout=3)
    client.ping()
    consumer = Consumer({'bootstrap.servers': args.bootstrap, 'group.id': args.group,
                         'auto.offset.reset': 'earliest', 'enable.auto.commit': False,
                         'enable.auto.offset.store': False})
    count = 0
    consumer.subscribe([args.topic])
    try:
        while args.max_events == 0 or count < args.max_events:
            message = consumer.poll(2.0)
            if message is None:
                continue
            if message.error():
                raise KafkaException(message.error())
            if message.topic() != args.topic or message.partition() != 0:
                raise ValueError('unexpected Topic partition')
            event = decrypt_event(message.value(), args.topic, key)
            seconds, micros = client.time()
            now_ms = seconds * 1000 + micros // 1000
            apply_change(client, event, prefix, config['source_timezone'], now_ms)
            consumer.commit(message=message, asynchronous=False)
            count += 1
            if count % 1000 == 0:
                print(json.dumps({'applied': count, 'offset': message.offset()}), flush=True)
    finally:
        consumer.close()
    print(json.dumps({'applied': count, 'status': 'stopped'}), flush=True)


if __name__ == '__main__':
    main()
