"""Single run controller: preflight -> HOLD -> final import -> COMMIT or ABORT."""
import argparse
import asyncio
import json
import fcntl
import os
import ssl
import time

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, UnixConnector

from .auth import expiry, fingerprint, project, replace_inactive, snapshot, verify
from .config import load, password


async def admin(config, command='status', payload=None):
    async with ClientSession(connector=UnixConnector(path=config['socket']),
                             timeout=ClientTimeout(total=2)) as session:
        if payload is None:
            request = session.get('http://localhost/' + command)
        else:
            request = session.post('http://localhost/' + command,
                                   json={'run_id': config['run_id'], **payload})
        async with request as response:
            result = await response.json()
            if response.status != 200:
                raise ValueError(result.get('error', 'control request rejected'))
            return result


def database(config):
    import pymysql
    c = config['mysql']
    return pymysql.connect(host=c['host'], port=c.get('port', 3306),
        user=c['user'], password=password(c), database=c['database'],
        autocommit=True, cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=2, read_timeout=2, write_timeout=2)


def redis_client(config):
    import redis
    c = config['redis']
    return redis.Redis(host=c['host'], port=c.get('port', 6379), db=c.get('db', 0),
        password=password(c), decode_responses=True, socket_timeout=1,
        socket_connect_timeout=1)


def checks(connection, config):
    with connection.cursor() as cursor:
        cursor.execute('SELECT DATABASE() AS db, @@read_only AS ro, @@super_read_only AS sro')
        row = cursor.fetchone()
        if row['db'] != config['mysql']['database'] or row['ro'] or row['sro']:
            raise ValueError('wrong schema or non-writer database')
        cursor.execute('''SELECT COUNT(*) AS n FROM trips t
            WHERE t.processing_status='PROCESSING' AND t.deleted_at IS NULL
            AND EXISTS (SELECT 1 FROM trip_attachments a
                        WHERE a.trip_id=t.trip_id AND a.deleted_at IS NULL)''')
        pending = int(cursor.fetchone()['n'])
        cursor.execute('''SELECT COUNT(*) AS n FROM files f
            JOIN trip_attachments a ON a.file_id=f.file_id
            JOIN trips t ON t.trip_id=a.trip_id
            WHERE t.processing_status='COMPLETED' AND t.deleted_at IS NULL
              AND a.deleted_at IS NULL AND f.deleted_at IS NULL
              AND f.original_size_bytes IS NULL''')
        missing = int(cursor.fetchone()['n'])
    if pending or missing:
        raise ValueError('unfinished legacy uploads/analysis or missing original sizes')
    return {'pending_jobs': pending, 'missing_original_sizes': missing}


def transfer(config):
    with database(config) as db:
        ready = checks(db, config)
        rows, source_now = snapshot(db)
    redis = redis_client(config)
    seconds, micros = redis.time()
    now = int(seconds * 1000 + micros / 1000)
    if abs(expiry(source_now, config['source_timezone']) - now) > 2000:
        raise ValueError('source/Redis clock or source timezone mismatch')
    entries = project(rows, config['redis']['prefix'], config['source_timezone'], now,
                      'migration-' + config['run_id'])
    replace_inactive(redis, entries, config['redis']['prefix'])
    seconds, micros = redis.time()
    check_at = int(seconds * 1000 + micros / 1000)
    verified = verify(redis, entries, config['redis']['prefix'], check_at)
    live = {k: v for k, v in entries.items() if v.expires > check_at}
    return {**ready, 'keys': verified['keys'], 'source_fingerprint': fingerprint(live),
            'target_fingerprint': verified['fingerprint']}


async def healthy(config):
    from aiohttp import TCPConnector
    async with ClientSession(cookie_jar=DummyCookieJar(), auto_decompress=False,
                             connector=TCPConnector(ssl=ssl.create_default_context(
                                 cafile=config.get('upstream_ca_file'))),
                             timeout=ClientTimeout(total=2)) as session:
        async with session.get(config['v2_origin'] + '/api/actuator/health',
                               allow_redirects=False,
                               server_hostname=config.get('v2_tls_name'),
                               headers={'Host': config['public_host']}) as response:
            if response.status != 200 or (await response.json()).get('status') != 'UP':
                raise ValueError('V2 health not ready')


async def _cutover(config):
    initial = await admin(config)
    if initial['phase'] != 'V1' or initial['waiters']:
        raise ValueError('rehearsal must start on V1')
    # Before the short boundary: schema/data/health checks can be slow and must not hold users.
    with database(config) as db:
        checks(db, config)
    await healthy(config)
    hold = await admin(config, 'begin', {})
    token = hold['hold_token']
    try:
        while True:
            state = await admin(config)
            if state['phase'] != 'HOLD' or state['hold_token'] != token:
                raise ValueError('hold aborted before final snapshot')
            if state['inflight']['V1'] == 0:
                break
            await asyncio.sleep(0.02)
        # Blocking driver work stays off the event loop. Gate has its own independent watchdog.
        proof = await asyncio.to_thread(transfer, config)
        return await admin(config, 'commit', {**proof, 'hold_token': token})
    except BaseException:
        state = await admin(config)
        if state['phase'] == 'HOLD' and state['hold_token'] == token:
            await admin(config, 'abort', {'hold_token': token})
        # A committed request may have rotated/revoked Redis state: never resume V1 automatically.
        raise


async def cutover(config):
    # A timed-out import may still be finishing in a thread. Serialize controllers
    # across processes so it cannot race with a later successful COMMIT.
    path = config['journal'] + '.controller.lock'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another controller/import is still running') from None
        return await _cutover(config)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('status', 'check', 'cutover', 'metrics'))
    parser.add_argument('--config', required=True)
    parser.add_argument('--confirm-run')
    args = parser.parse_args()
    config = load(args.config)
    try:
        if args.command == 'cutover':
            if args.confirm_run != config['run_id']:
                raise ValueError('cutover requires matching --confirm-run')
            result = asyncio.run(cutover(config))
        elif args.command == 'check':
            with database(config) as db:
                result = checks(db, config)
            asyncio.run(healthy(config))
            result.update(asyncio.run(admin(config)))
        elif args.command == 'metrics':
            async def metrics():
                async with ClientSession(connector=UnixConnector(path=config['socket'])) as session:
                    async with session.get('http://localhost/metrics') as response:
                        return await response.text()
            print(asyncio.run(metrics()), end='')
            return
        else:
            result = asyncio.run(admin(config))
        # Hold nonce is a local control value, not an authentication credential.
        print(json.dumps(result, separators=(',', ':')))
    except Exception as error:
        # Driver messages may embed credentials or token values. Print type, never raw driver text.
        print(json.dumps({'result': 'failed', 'error_type': type(error).__name__,
                          'message': str(error) if isinstance(error, ValueError) else 'inspect private service diagnostics'}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
