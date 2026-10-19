"""One-off production AI Redis checks; never print credentials or full URLs."""
import json
import os
import re
import socket
from urllib.parse import urlsplit


def command(stream, *parts):
    encoded = [str(part).encode() for part in parts]
    stream.write(b'*%d\r\n' % len(encoded) + b''.join(b'$%d\r\n' % len(part) + part + b'\r\n' for part in encoded))
    stream.flush()
    line = stream.readline()
    if not line.endswith(b'\r\n'):
        raise RuntimeError('Incomplete Redis reply')
    kind, payload = line[:1], line[1:-2]
    if kind == b'-':
        return ('error', payload)
    if kind == b'+':
        return payload
    if kind == b':':
        return int(payload)
    if kind == b'$':
        size = int(payload)
        if size == -1:
            return None
        value = stream.read(size)
        if stream.read(2) != b'\r\n':
            raise RuntimeError('Incomplete Redis bulk reply')
        return value
    raise RuntimeError('Unexpected Redis reply type')


def main():
    mode = os.environ['PROBE_MODE']
    host = 'ai-redis.production.yeodam.internal'
    address = socket.gethostbyname(host)
    assert address == os.environ['EXPECTED_REDIS_IP'], 'Wrong private DNS destination'
    if mode == 'deny':
        try:
            with socket.create_connection((address, 6379), timeout=5):
                raise RuntimeError('Unexpected Redis access from non-AI security group')
        except (TimeoutError, socket.timeout):
            print(json.dumps({'check': 'nonAiConnectionBlocked', 'passed': True}))
        return
    assert mode == 'allow'
    run = os.environ['PROBE_RUN_ID']
    assert re.fullmatch('[0-9a-f]{32}', run)
    url = urlsplit(os.environ['REDIS_URL'])
    assert url.scheme == 'redis' and url.hostname == host and url.port == 6379 and url.path == '/0'
    assert re.fullmatch('[0-9a-f]{64}', url.password or '')
    key = 'cloud:preflight:332:' + run
    with socket.create_connection((address, 6379), timeout=5) as connection:
        with connection.makefile('rwb') as stream:
            reply = command(stream, 'PING')
            assert isinstance(reply, tuple) and reply[0] == 'error' and reply[1].startswith(b'NOAUTH')
            assert command(stream, 'AUTH', url.password) == b'OK'
            assert command(stream, 'PING') == b'PONG'
            assert command(stream, 'GET', key) == run.encode(), 'Restart marker missing'
            secondary = key + ':client'
            try:
                assert command(stream, 'SET', secondary, run, 'EX', 900) == b'OK'
                assert command(stream, 'GET', secondary) == run.encode()
            finally:
                command(stream, 'DEL', secondary)
            assert command(stream, 'DEL', key) == 1
    print(json.dumps({'check': 'aiClientAuthReadWrite', 'persistedMarkerAfterRestart': True, 'testKeysRemoved': True, 'passed': True}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit('AI Redis verification failed: ' + type(error).__name__) from None
