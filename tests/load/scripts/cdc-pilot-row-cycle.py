#!/usr/bin/env python3
"""A scoped SQL create/update/delete cycle for the B1 ordered CDC pilot."""
import argparse
import hashlib
import re
import subprocess
import sys
from uuid import UUID

SCHEMA = 'yeodam_v1_source'
USER_ID = 707  # Existing seeded user whose presence was verified in both databases.


def mysql(statement):
    result = subprocess.run(['mysql', '--protocol=socket', '-uroot',
                             '--default-character-set=utf8mb4', '-N', '-B',
                             '--database=' + SCHEMA], input=statement, text=True,
                            capture_output=True)
    if result.returncode:
        raise RuntimeError('Source SQL failed; inspect locally without printing credentials')
    return result.stdout.strip()


def trip_id(name):
    found = mysql(f"SELECT trip_id FROM trips WHERE user_id={USER_ID} AND trip_name='{name}' ORDER BY trip_id;")
    if not re.fullmatch(r'[0-9]+', found):
        raise RuntimeError('Expected exactly one scoped pilot trip')
    return int(found)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('create', 'update', 'delete'))
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    if str(UUID(args.run_id)) != args.run_id:
        raise ValueError('canonical run ID required')
    name = 'b304s' + hashlib.sha256(args.run_id.encode()).hexdigest()[:5]
    if args.phase == 'create':
        if mysql(f"SELECT COUNT(*) FROM trips WHERE user_id={USER_ID} AND trip_name='{name}';") != '0':
            raise RuntimeError('Pilot trip already exists; refusing duplicate create')
        result = mysql("START TRANSACTION; "
                       f"INSERT INTO trips(user_id,trip_name,start_date,end_date) VALUES({USER_ID},'{name}','2025-01-01','2025-01-03'); "
                       "SET @pilot_trip_id=LAST_INSERT_ID(); "
                       "INSERT INTO trip_regions(trip_id,region_code,region_name,latitude,longitude) "
                       "VALUES(@pilot_trip_id,'50110','pilot-region',33.50000000,126.50000000); "
                       "COMMIT; SELECT @pilot_trip_id;")
        if not re.fullmatch(r'[0-9]+', result):
            raise RuntimeError('Created trip ID missing')
        print('ROW_CYCLE created trip_id=' + result + ' name=' + name)
    elif args.phase == 'update':
        row = trip_id(name)
        mysql(f"UPDATE trips SET is_favorite=1 WHERE trip_id={row} AND is_favorite=0;")
        if mysql(f'SELECT is_favorite FROM trips WHERE trip_id={row};') != '1':
            raise RuntimeError('Pilot update not visible on Source')
        print('ROW_CYCLE updated trip_id=' + str(row))
    else:
        row = trip_id(name)
        if mysql(f'SELECT COUNT(*) FROM trip_regions WHERE trip_id={row};') != '1':
            raise RuntimeError('Expected exactly one scoped region')
        mysql(f'START TRANSACTION; DELETE FROM trip_regions WHERE trip_id={row}; '
              f'DELETE FROM trips WHERE trip_id={row}; COMMIT;')
        if mysql(f'SELECT COUNT(*) FROM trips WHERE trip_id={row};') != '0':
            raise RuntimeError('Pilot delete not visible on Source')
        print('ROW_CYCLE deleted trip_id=' + str(row))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError) as error:
        print('ROW_CYCLE_FAILED ' + type(error).__name__ + ' ' + str(error)[:120], file=sys.stderr)
        raise SystemExit(1)
