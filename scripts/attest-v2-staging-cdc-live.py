#!/usr/bin/env python3
"""Hash every field of the #313 three-table dataset on its own DB host."""

import argparse
import hashlib
import json
import subprocess


TABLES = {
    'users': ('user_id', 'email', 'nickname', 'profile_image_url',
              'created_at', 'updated_at', 'deleted_at'),
    'trips': ('trip_id', 'user_id', 'trip_name', 'start_date', 'end_date',
              'is_favorite', 'thumbnail_key', 'processing_status',
              'created_at', 'updated_at', 'deleted_at', 'current_story_id'),
    'trip_regions': ('region_id', 'trip_id', 'region_code', 'region_name',
                     'latitude', 'longitude', 'created_at', 'updated_at', 'deleted_at'),
}


def command(role):
    if role == 'source':
        return ['mysql', '--protocol=socket', '-uroot', '--quick', '--batch',
                '--skip-column-names']
    return ['docker', 'exec', '-i', 'yeodam-mysql-cdc-test', 'sh', '-ec',
            'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; '
            'exec mysql -uroot --quick --batch --skip-column-names']


def query(role, sql):
    result = subprocess.run(command(role), input=sql, text=True,
                            capture_output=True, check=True)
    return result.stdout.strip()


def attest(role, table):
    schema = 'yeodam_v1_source' if role == 'source' else 'yeodam_cdc_live313'
    pk = TABLES[table][0]
    columns = ','.join("IFNULL(HEX(CAST(`%s` AS CHAR CHARACTER SET utf8mb4)),'~')" % col
                       for col in TABLES[table])
    sql = ("SET NAMES utf8mb4; SELECT CONCAT_WS('|',%s) FROM `%s`.`%s` ORDER BY `%s`;\n"
           % (columns, schema, table, pk))
    process = subprocess.Popen(command(role), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process.stdin.write(sql.encode())
    process.stdin.close()
    digest = hashlib.sha256()
    count = 0
    for line in process.stdout:
        digest.update(line)
        count += 1
    process.stdout.close()
    error = process.stderr.read()
    exit_code = process.wait()
    if exit_code:
        raise RuntimeError('MySQL attestation failed: ' + error.decode(errors='replace')[:200])
    return {'rows': count, 'sha256': digest.hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('role', choices=('source', 'destination'))
    args = parser.parse_args()
    role = args.role
    server_id = int(query(role, 'SELECT @@server_id;'))
    if server_id != (1 if role == 'source' else 300):
        raise RuntimeError('unexpected MySQL server_id')
    result = {'role': role, 'serverId': server_id, 'tables': {}}
    for table in TABLES:
        result['tables'][table] = attest(role, table)
    if role == 'destination':
        result['foreignKeyOrphans'] = {
            'tripsWithoutUser': int(query(role,
                'SELECT COUNT(*) FROM yeodam_cdc_live313.trips t '
                'LEFT JOIN yeodam_cdc_live313.users u ON u.user_id=t.user_id '
                'WHERE u.user_id IS NULL;')),
            'regionsWithoutTrip': int(query(role,
                'SELECT COUNT(*) FROM yeodam_cdc_live313.trip_regions r '
                'LEFT JOIN yeodam_cdc_live313.trips t ON t.trip_id=r.trip_id '
                'WHERE t.trip_id IS NULL;')),
        }
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
