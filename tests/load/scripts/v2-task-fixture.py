#!/usr/bin/env python3
"""Scoped fixture operations on the staging QA MySQL host, using its root socket."""
import argparse
import hashlib
import json
import subprocess
import uuid


def query(sql):
    result = subprocess.run(['docker', 'exec', '-i', 'yeodam-v2-mysql', 'sh', '-c',
        'MYSQL_PWD=$(cat /run/secrets/mysql-root-password) exec mysql -uroot '
        '--database=yeodam_validation --batch --raw --skip-column-names'],
        input=sql, capture_output=True, text=True)
    if result.returncode: raise RuntimeError('Scoped MySQL operation failed; SQL output withheld')
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'reset', 'audit', 'cleanup', 'reset-to-seed'])
    parser.add_argument('--run-id', required=True, type=uuid.UUID)
    parser.add_argument('--exclude-seed', action='store_true')
    args = parser.parse_args()
    prefix = 'load474-' + str(args.run_id) + '-'
    owned = "email LIKE '" + prefix + "%@staging.invalid'"
    if query('SELECT @@read_only;') != '0': raise ValueError('QA DB not writable')
    if args.action == 'prepare':
        if query('SELECT COUNT(*) FROM users WHERE ' + owned + ';') != '0':
            raise ValueError('Fixture already exists')
        values = ','.join("('%s%03d@staging.invalid','t474%03d')" % (prefix, n, n)
                          for n in range(100))
        query('START TRANSACTION; INSERT INTO users(email,nickname) VALUES ' + values + ';'
            ' INSERT INTO user_stats(user_id) SELECT user_id FROM users WHERE ' + owned + '; COMMIT;')
        ids = query('SELECT user_id FROM users WHERE ' + owned + ' ORDER BY user_id;')
        print(json.dumps({'runId': str(args.run_id), 'users': [int(i) for i in ids.splitlines()]}))
        return
    if query('SELECT COUNT(*) FROM users WHERE ' + owned + ';') != '100':
        raise ValueError('Expected exactly 100 owned users')
    if args.action == 'audit':
        extra = " AND t.trip_name <> 'seed474'" if args.exclude_seed else ''
        rows = query('SELECT t.trip_id,t.user_id,t.trip_name,r.region_code FROM trips t '
            'JOIN users u ON u.user_id=t.user_id JOIN trip_regions r ON r.trip_id=t.trip_id '
            'WHERE u.' + owned + extra + ' ORDER BY t.trip_id,r.region_code;')
        trip_count = int(query('SELECT COUNT(*) FROM trips t JOIN users u ON u.user_id=t.user_id '
                              'WHERE u.' + owned + extra + ';'))
        row_count = len(rows.splitlines()) if rows else 0
        canonical = (rows + '\n').encode() if rows else b''
        orphans = int(query('SELECT COUNT(*) FROM trip_regions r LEFT JOIN trips t '
            'ON t.trip_id=r.trip_id WHERE t.trip_id IS NULL;'))
        seed_count = int(query('SELECT COUNT(*) FROM trips t JOIN users u ON u.user_id=t.user_id '
                              'WHERE u.' + owned + " AND t.trip_name='seed474';"))
        print(json.dumps({'trips': trip_count, 'regions': row_count, 'seedTrips': seed_count,
                          'globalOrphanRegions': orphans,
                          'projectionSHA256': hashlib.sha256(canonical).hexdigest()}))
        return
    # Nothing is dropped/truncated; only this UUID's synthetic users and their metadata.
    extra = " AND t.trip_name <> 'seed474'" if args.action == 'reset-to-seed' else ''
    if args.action == 'reset-to-seed' and query('SELECT COUNT(*) FROM trips t '
            'JOIN users u ON u.user_id=t.user_id WHERE u.' + owned + " AND t.trip_name='seed474';") != '100':
        raise ValueError('Expected one preserved seed per synthetic user')
    sql = ('START TRANSACTION; DELETE r FROM trip_regions r JOIN trips t ON t.trip_id=r.trip_id '
        'JOIN users u ON u.user_id=t.user_id WHERE u.' + owned + extra + ';'
        ' DELETE t FROM trips t JOIN users u ON u.user_id=t.user_id WHERE u.' + owned + extra + ';'
        ' UPDATE user_stats s JOIN users u ON u.user_id=s.user_id SET trip_count=0 '
        'WHERE u.' + owned + ';')
    if args.action == 'cleanup':
        sql += (' DELETE s FROM user_stats s JOIN users u ON u.user_id=s.user_id WHERE u.' + owned + ';'
                ' DELETE FROM users WHERE ' + owned + ';')
    query(sql + ' COMMIT;')
    print(json.dumps({'action': args.action, 'ownedUsers':
        int(query('SELECT COUNT(*) FROM users WHERE ' + owned + ';'))}))


if __name__ == '__main__': main()
