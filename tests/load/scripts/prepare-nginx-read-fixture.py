#!/usr/bin/env python3
"""V1 staging only: new synthetic owner and read-only UI fixtures from baseline photos.

Only INSERTs new fixture rows. Existing owners, objects and trips are untouched.
Never use attachment deletion APIs to clean this fixture: S3 keys are shared.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
from functools import cache
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('pool', Path(__file__).with_name('synthetic-session-pool.py'))
pool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pool)


@cache
def columns(table, pk):
    rows = pool.run_mysql('SHOW COLUMNS FROM '+table+';').splitlines()
    names = [r.split('\t')[0] for r in rows]
    if any(not re.fullmatch('[a-z_]+', n) for n in names) or pk not in names:
        raise ValueError('Unexpected schema')
    return [n for n in names if n != pk]


def clone(table, pk, condition, changes):
    names = columns(table, pk)
    if not changes.keys() <= set(names):
        raise ValueError('Fixture column differs from deployed schema')
    return ('INSERT INTO '+table+' ('+','.join(names)+') SELECT '
            +','.join(changes.get(n, '`'+n+'`') for n in names)
            +' FROM '+table+' WHERE '+condition+';')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id', type=pool.run_uuid, required=True)
    a = p.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0 or Path('/var/lib/cloud/data/instance-id').read_text().strip() != 'i-05d320e3a87200d5a':
        raise ValueError('Root on V1 staging App only')
    if pool.run_mysql('SELECT @@read_only,@@super_read_only;') != '0\t0':
        raise ValueError('Source is fenced; no fixture writes')
    if pool.run_mysql('SELECT COUNT(*) FROM trips WHERE trip_id IN (2048043,2048045) AND processing_status="COMPLETED" AND deleted_at IS NULL;') != '2':
        raise ValueError('Expected completed baseline trips are missing')
    private = Path('/run/yeodam-nginx-read-fixture')
    private.mkdir(mode=0o700, exist_ok=True)
    output = private/(a.run_id+'.json')
    metadata = private/(a.run_id+'.fixture.json')
    if metadata.exists() or output.exists():
        raise ValueError('Fixture run already exists; use a new UUID')
    pool.prepare(SimpleNamespace(run_id=a.run_id, count=1, output=str(output)))
    user_id = int(json.loads(output.read_text())['sessions'][0]['userId'])
    sql = ['START TRANSACTION;']
    for i in range(1, 13):
        source = 2048045 if i == 1 else 2048043
        sql.append(clone('trips', 'trip_id', f'trip_id={source} AND deleted_at IS NULL',
                         {'user_id': str(user_id), 'trip_name': f"'read416-{i:02d}'", 'is_favorite': '1' if i % 2 else '0'}))
        sql.append(f'SET @t{i}=LAST_INSERT_ID();')
        sql.append(clone('trip_regions', 'region_id', f'trip_id={source} AND deleted_at IS NULL', {'trip_id': f'@t{i}'}))
        original_place = int(pool.run_mysql(f'SELECT MIN(trip_place_id) FROM trip_detail_places WHERE trip_id={source} AND deleted_at IS NULL;'))
        for folder in range(1, 14 if i == 1 else 2):
            sql.append(clone('trip_detail_places', 'trip_place_id', f'trip_place_id={original_place}',
                             {'trip_id': f'@t{i}', 'place_name': f"'read-place-{folder:02d}'", 'order_number': str(folder)}))
            if folder == 1:
                sql.append(f'SET @p{i}=LAST_INSERT_ID();')
        attachments = pool.run_mysql(f'SELECT trip_attachment_id,file_id FROM trip_attachments WHERE trip_id={source} AND deleted_at IS NULL ORDER BY trip_attachment_id;')
        expected = 150 if i == 1 else 10
        if len(attachments.splitlines()) != expected:
            raise ValueError('Baseline attachment count differs')
        for row in attachments.splitlines():
            attachment_id, file_id = map(int, row.split('\t'))
            sql.append(clone('files', 'file_id', f'file_id={file_id} AND deleted_at IS NULL', {'user_id': str(user_id)}))
            sql.append('SET @f=LAST_INSERT_ID();')
            sql.append(clone('trip_attachments', 'trip_attachment_id', f'trip_attachment_id={attachment_id}',
                             {'trip_id': f'@t{i}', 'trip_place_id': f'@p{i}', 'file_id': '@f',
                              'classification_status': "'ACTIVE'", 'issue': "'NONE'"}))
    sql.append('COMMIT; SELECT @t1,@p1;')
    trip_id, folder_id = map(int, pool.run_mysql(''.join(sql)).splitlines()[-1].split('\t'))
    result = {'runId': a.run_id, 'userId': user_id, 'mainTripId': trip_id,
              'mainFolderId': folder_id, 'tripCount': 12, 'mainFolderCount': 13,
              'mainPhotoCount': 150, 'totalPhotos': 260, 'privatePoolFile': str(output),
              'objectsSharedWithBaseline': True, 'AIExecuted': False}
    metadata.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
