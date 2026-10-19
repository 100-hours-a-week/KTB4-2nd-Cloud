"""One-off check from the future Backend SG; no existing production data is used."""
import json
import os
import socket
import ssl
import uuid

import pymysql


def check():
    mysql_host = os.environ['MYSQL_HOST'].rstrip('.')
    redis_host = os.environ['REDIS_HOST']
    assert mysql_host == 'mysql-predeploy.production.yeodam.internal'
    assert redis_host.startswith('master.yeodam-v2-production-redis.')
    assert redis_host.endswith('.apn2.cache.amazonaws.com')
    conn = pymysql.connect(host=mysql_host, user='yeodam_app',
                           password=os.environ['MYSQL_PASSWORD'], database='yeodam',
                           connect_timeout=8, read_timeout=8, write_timeout=8)
    table = '_cloud_preflight_' + uuid.uuid4().hex
    marker = uuid.uuid4().hex
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT VERSION(), CURRENT_USER()')
            version, user = cur.fetchone()
            assert version == '9.7.2' and user == 'yeodam_app@10.20.10.%'
            cur.execute('SHOW GRANTS')
            grants = [row[0] for row in cur.fetchall()]
            assert all('ON `yeodam`.*' in grant or grant.startswith('GRANT USAGE ON *.*')
                       for grant in grants)
            cur.execute(f'CREATE TABLE `{table}` (id INT PRIMARY KEY, marker VARCHAR(32) NOT NULL)')
            cur.execute(f'INSERT INTO `{table}` VALUES (1,%s)', (marker,))
            conn.commit()
            cur.execute(f'SELECT marker FROM `{table}` WHERE id=1')
            assert cur.fetchone()[0] == marker
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS `{table}`')
        conn.close()

    context = ssl.create_default_context()
    def send(stream, *parts):
        fields = [str(p).encode() for p in parts]
        stream.write(b'*' + str(len(fields)).encode() + b'\r\n' + b''.join(
            b'$' + str(len(p)).encode() + b'\r\n' + p + b'\r\n' for p in fields))
        stream.flush()
        return stream.readline()

    key = 'yeodam:v2:production:preflight:' + uuid.uuid4().hex
    with socket.create_connection((redis_host, 6379), timeout=8) as tcp:
        with context.wrap_socket(tcp, server_hostname=redis_host) as tls:
            with tls.makefile('rwb') as stream:
                assert send(stream, 'PING').startswith(b'-NOAUTH')
                assert send(stream, 'AUTH', os.environ['REDIS_PASSWORD']) == b'+OK\r\n'
                assert send(stream, 'PING') == b'+PONG\r\n'
                try:
                    assert send(stream, 'SET', key, 'ok', 'EX', 60, 'NX') == b'+OK\r\n'
                    assert send(stream, 'GET', key) == b'$2\r\n'
                    assert stream.readline() == b'ok\r\n'
                finally:
                    assert send(stream, 'DEL', key) in [b':1\r\n', b':0\r\n']
    print(json.dumps({'mysql': 'authenticated read/write and schema-scoped grant passed',
                      'redis': 'verified TLS, unauthenticated rejection, AUTH and read/write passed',
                      'scratchDataRemoved': True}))


if __name__ == '__main__':
    try:
        check()
    except Exception as error:
        print(json.dumps({'passed': False, 'errorType': type(error).__name__}))
        raise SystemExit(1)
