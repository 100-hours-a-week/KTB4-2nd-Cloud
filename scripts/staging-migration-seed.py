#!/usr/bin/env python3
"""Guarded, resumable V1 staging trip dataset loader. Never targets QA or production."""

import argparse
from datetime import date, timedelta
import fcntl
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from uuid import UUID


SCHEMA = 'yeodam_v1_source'
BASE = Path('/var/lib/yeodam/migration-seed')
BUCKET = 'yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2'
SOURCE_IP = '10.30.0.174'
TARGET_IP = '10.30.10.28'
REGION = ('12110', '전남광주통합특별시 목포시', '34.81183093', '126.39223317')
MIN_FREE_BYTES = 8 * 1024**3


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def run(args, data=None, timeout=120):
    result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=timeout)
    require(result.returncode == 0, 'Command failed; preserve dataset state and inspect the host')
    return result.stdout.strip()


def mysql(sql, timeout=120):
    return run(['mysql', '--protocol=socket', '-uroot', '--database='+SCHEMA,
                '--batch', '--skip-column-names'], sql, timeout)


def scalar(sql):
    return mysql(sql+';').splitlines()[-1]


def identity():
    return mysql('SELECT @@server_id,@@server_uuid,@@read_only,@@super_read_only;').split('\t')


def binlog():
    fields = mysql('SHOW BINARY LOG STATUS;').split('\t')
    require(len(fields) >= 2 and re.fullmatch(r'binlog\.[0-9]+', fields[0]), 'Invalid Source binlog')
    return [fields[0], int(fields[1])]


def free_bytes():
    volume = os.statvfs('/var/lib/mysql')
    return volume.f_bavail * volume.f_frsize


def persist(path, value):
    fd, temp = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def cutover_db():
    path = '/usr/local/sbin/yeodam-staging-db-cutover'
    require(Path(path).is_file(), 'Cutover read-only client is not installed')
    spec = importlib.util.spec_from_loader('seed_cutover', SourceFileLoader('seed_cutover', path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Database(TARGET_IP), module


def preflight():
    require(os.geteuid() == 0, 'Run as root on the V1 staging Source')
    require(SOURCE_IP in run(['hostname', '-I']).split(), 'Wrong Source private IP')
    require(run(['aws','sts','get-caller-identity','--region','ap-northeast-2',
                 '--query','Account','--output','text']) == '483175530259', 'Wrong AWS account')
    source = identity()
    require(source[0] == '1' and source[2:] == ['0','0'], 'Source is not the current Writer')
    active = Path('/var/lib/yeodam/migration-cutover/active.json')
    require(not active.exists(), 'Active cutover Journal needs inspection')
    reset = Path('/var/lib/yeodam/rehearsal-reset/active.json')
    require(not reset.exists() or json.loads(reset.read_text()).get('phase') == 'completed',
            'An incomplete reset exists')
    require(not run(['docker','ps','--filter','label=com.docker.compose.project=yeodam-app',
                     '--filter','label=com.docker.compose.service=backend','--quiet']),
            'Stop the V1 staging Backend before seeding')
    db, module = cutover_db()
    try:
        target = db.identity('target')
        status = db.status()
        require(target[0] == '2' and target[2:] == ['1','1'], 'Target is not a protected Replica')
        require(status['Source_UUID'] == source[1] and status['Source_Host'] == SOURCE_IP
                and status['Replica_IO_Running'] == 'Yes' and status['Replica_SQL_Running'] == 'Yes'
                and status['Last_IO_Errno'] == '0' and status['Last_SQL_Errno'] == '0'
                and status['Replicate_Wild_Do_Table'] == SCHEMA+'.%'
                and status['Auto_Position'] == '0', 'Unexpected replica state')
        require(db.query('proxy', "SELECT DISTINCT default_hostgroup FROM runtime_mysql_users WHERE username='yeodam_app';") == '10',
                'ProxySQL does not point to Source')
    finally:
        db.close()
    require(free_bytes() >= MIN_FREE_BYTES,
            'Source data volume has less than 8 GiB free')
    return source, binlog()


def snapshot_marker(key, source_uuid):
    require(re.fullmatch(r'migration/v1-source/[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\.ready', key),
            'Invalid pre-seed snapshot marker')
    content = run(['aws','s3','cp',f's3://{BUCKET}/{key}','-',
                   '--region','ap-northeast-2','--only-show-errors'])
    marker = dict(line.split('=',1) for line in content.splitlines() if '=' in line)
    require(marker.get('source_schema') == SCHEMA and marker.get('source_uuid') == source_uuid
            and re.fullmatch(r'[0-9a-f]{64}', marker.get('sha256',''))
            and marker.get('snapshot_key') == key.removesuffix('.ready')+'.sql.gz',
            'Snapshot does not match current Source')
    actual_size = run(['aws','s3api','head-object','--bucket',BUCKET,
                       '--key',marker['snapshot_key'],'--region','ap-northeast-2',
                       '--query','ContentLength','--output','text'])
    require(actual_size == marker.get('compressed_bytes') and actual_size.isdigit()
            and int(actual_size) > 0, 'Snapshot object is missing or differs from marker')
    stamp = marker.get('created_at_utc','').split('-')[0]
    require(re.fullmatch(r'[0-9]{8}T[0-9]{6}Z', stamp), 'Invalid snapshot time')
    import datetime
    created = datetime.datetime.strptime(stamp, '%Y%m%dT%H%M%SZ').replace(tzinfo=datetime.timezone.utc)
    require(0 <= (datetime.datetime.now(datetime.timezone.utc)-created).total_seconds() <= 3600,
            'Pre-seed snapshot must be less than one hour old')
    return marker


def wait_target(position):
    db, module = cutover_db()
    try:
        file, offset = position
        waited = db.query('target',
                          f"SELECT SOURCE_POS_WAIT('{file}',{offset},60,'v1_to_v2');",
                          timeout=65)
        require(waited.isdigit() and module.caught_up(db.status(), position),
                'Target did not apply the completed Source batch; preserve the checkpoint')
    finally:
        db.close()


def email(dataset_id, user_index):
    return f'mig-data-{dataset_id.replace("-", "")}-{user_index:05d}@staging.invalid'


def batch_sql(state, batch):
    size = state['batchSize']
    start = batch * size
    stop = min(start+size, state['tripCount'])
    first_user = start // 100
    last_user = (stop-1) // 100
    users = []
    trips = []
    regions = []
    for i in range(first_user, last_user+1):
        users.append(f"({state['firstUserId']+i},'{email(state['datasetId'],i)}','seed')")
    for i in range(start, stop):
        trip_id = state['firstTripId']+i
        user_id = state['firstUserId']+(i//100)
        day = date(2025,1,1)+timedelta(days=i%365)
        end = day+timedelta(days=i%7)
        trips.append(f"({trip_id},{user_id},'s{i%100:03d}','{day}','{end}','COMPLETED')")
        regions.append(f"({state['firstRegionId']+i},{trip_id},'{REGION[0]}','{REGION[1]}',{REGION[2]},{REGION[3]})")
    return ('START TRANSACTION;\n'
            'INSERT INTO users(user_id,email,nickname) VALUES '+','.join(users)+';\n'
            'INSERT INTO trips(trip_id,user_id,trip_name,start_date,end_date,processing_status) VALUES '+','.join(trips)+';\n'
            'INSERT INTO trip_regions(region_id,trip_id,region_code,region_name,latitude,longitude) VALUES '+','.join(regions)+';\n'
            'COMMIT;\n')


def batch_counts(state, batch):
    start = batch * state['batchSize']
    stop = min(start+state['batchSize'], state['tripCount'])
    lo_user = state['firstUserId']+start//100
    hi_user = state['firstUserId']+(stop-1)//100
    lo_trip = state['firstTripId']+start
    hi_trip = state['firstTripId']+stop-1
    lo_region = state['firstRegionId']+start
    hi_region = state['firstRegionId']+stop-1
    counts = mysql(f"SELECT (SELECT COUNT(*) FROM users WHERE user_id BETWEEN {lo_user} AND {hi_user}),"
                   f"(SELECT COUNT(*) FROM trips WHERE trip_id BETWEEN {lo_trip} AND {hi_trip}),"
                   f"(SELECT COUNT(*) FROM trip_regions WHERE region_id BETWEEN {lo_region} AND {hi_region});")
    return [int(x) for x in counts.split('\t')]


def batch_integrity(state, batch):
    """Reject another writer's rows even if all three ID ranges look full."""
    start = batch * state['batchSize']
    stop = min(start+state['batchSize'], state['tripCount'])
    user_start = start//100
    user_stop = (stop-1)//100
    first_user = state['firstUserId']
    first_trip = state['firstTripId']
    first_region = state['firstRegionId']
    prefix = 'mig-data-'+state['datasetId'].replace('-','')+'-'
    users_ok = scalar(
        f"SELECT COUNT(*) FROM users WHERE user_id BETWEEN {first_user+user_start} AND {first_user+user_stop} "
        f"AND email=CONCAT('{prefix}',LPAD(user_id-{first_user},5,'0'),'@staging.invalid') AND nickname='seed'")
    trips_ok = scalar(
        f"SELECT COUNT(*) FROM trips WHERE trip_id BETWEEN {first_trip+start} AND {first_trip+stop-1} "
        f"AND user_id={first_user}+FLOOR((trip_id-{first_trip})/100) "
        f"AND trip_name=CONCAT('s',LPAD(MOD(trip_id-{first_trip},100),3,'0')) "
        f"AND start_date=DATE_ADD('2025-01-01',INTERVAL MOD(trip_id-{first_trip},365) DAY) "
        f"AND end_date=DATE_ADD(start_date,INTERVAL MOD(trip_id-{first_trip},7) DAY) "
        "AND processing_status='COMPLETED' AND current_story_id IS NULL")
    regions_ok = scalar(
        f"SELECT COUNT(*) FROM trip_regions WHERE region_id BETWEEN {first_region+start} AND {first_region+stop-1} "
        f"AND trip_id={first_trip}+(region_id-{first_region}) AND region_code='{REGION[0]}' "
        f"AND region_name='{REGION[1]}' AND latitude={REGION[2]} AND longitude={REGION[3]}")
    require([int(users_ok),int(trips_ok),int(regions_ok)] ==
            [user_stop-user_start+1,stop-start,stop-start],
            f'Batch {batch} rows do not match the dataset contract')


def run_load(args):
    require(str(UUID(args.dataset_id)) == args.dataset_id, 'Canonical Dataset UUID required')
    require(args.confirm_load == args.dataset_id, 'Matching confirmation UUID required')
    require(args.trip_count == 1_000_000 and args.users == 10_000 and args.batch_size == 10_000,
            'This Issue supports exactly 10,000 users, 1,000,000 trips and 10,000-trip chunks')
    source, source_pos = preflight()
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    BASE.chmod(0o700)
    directory = BASE/args.dataset_id
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory/'state.json'
    with (BASE/'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        if path.exists():
            state = json.loads(path.read_text())
            require(state['datasetId'] == args.dataset_id and state['snapshotReadyKey'] == args.snapshot_ready_key
                    and state['sourceUuid'] == source[1] and state['tripCount'] == args.trip_count
                    and state['userCount'] == args.users and state['batchSize'] == args.batch_size,
                    'Dataset checkpoint mismatch')
        else:
            marker = snapshot_marker(args.snapshot_ready_key, source[1])
            require([marker['source_file'],int(marker['source_position'])] <= source_pos,
                    'Snapshot position is ahead of Source')
            require(scalar("SELECT COUNT(*) FROM users WHERE email LIKE 'mig-data-%@staging.invalid'") == '0',
                    'Another seed dataset already exists')
            maximums = [int(x) for x in mysql('SELECT (SELECT COALESCE(MAX(user_id),0) FROM users),'
                                               '(SELECT COALESCE(MAX(trip_id),0) FROM trips),'
                                               '(SELECT COALESCE(MAX(region_id),0) FROM trip_regions);').split('\t')]
            state = {'datasetId':args.dataset_id,'sourceUuid':source[1],
                     'snapshotReadyKey':args.snapshot_ready_key,'tripCount':args.trip_count,
                     'userCount':args.users,'batchSize':args.batch_size,
                     'firstUserId':maximums[0]+1,'firstTripId':maximums[1]+1,
                     'firstRegionId':maximums[2]+1,'completedBatches':[],
                     'startedAt':time.time(),'startPosition':source_pos}
            persist(path,state)
        total = (state['tripCount']+state['batchSize']-1)//state['batchSize']
        require(state['completedBatches'] == list(range(len(state['completedBatches'])))
                and len(state['completedBatches']) <= total, 'Non-contiguous dataset checkpoint')
        limit = min(total, len(state['completedBatches'])+args.max_batches)
        if state.get('lastPosition'):
            wait_target(state['lastPosition'])
        for batch in range(total):
            if batch in state['completedBatches']:
                continue
            if batch >= limit:
                break
            started = time.monotonic()
            before_free = free_bytes()
            preflight()
            expected = [state['batchSize']//100,state['batchSize'],state['batchSize']]
            observed = batch_counts(state,batch)
            require(observed in ([0,0,0],expected), f'Partial or collided batch {batch}: {observed}')
            if observed == [0,0,0]:
                mysql(batch_sql(state,batch),timeout=600)
                require(batch_counts(state,batch) == expected, f'Batch {batch} did not commit in full')
            batch_integrity(state,batch)
            state['completedBatches'].append(batch)
            state['lastPosition'] = binlog()
            state['lastCompletedAt'] = time.time()
            persist(path,state)
            wait_target(state['lastPosition'])
            print(json.dumps({'datasetId':args.dataset_id,'batch':batch,'completed':len(state['completedBatches']),
                              'total':total,'position':state['lastPosition'],
                              'batchSeconds':round(time.monotonic()-started,3),
                              'sourceFreeBytesBefore':before_free,
                              'sourceFreeBytesAfter':free_bytes()}),flush=True)
        print(json.dumps({'datasetId':args.dataset_id,'completedBatches':len(state['completedBatches']),
                          'totalBatches':total,'statePath':str(path)}),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset-id',required=True)
    parser.add_argument('--snapshot-ready-key',required=True)
    parser.add_argument('--confirm-load',required=True)
    parser.add_argument('--trip-count',type=int,default=1_000_000)
    parser.add_argument('--users',type=int,default=10_000)
    parser.add_argument('--batch-size',type=int,default=10_000)
    parser.add_argument('--max-batches',type=int,default=1)
    args = parser.parse_args()
    try:
        require(1 <= args.max_batches <= 10, 'Run at most ten batches per invocation')
        run_load(args)
    except (RuntimeError,ValueError,OSError,KeyError,subprocess.TimeoutExpired) as error:
        print(f'ERROR: {error}',file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
