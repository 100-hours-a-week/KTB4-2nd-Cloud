#!/usr/bin/env python3
"""Apply the approved trip lookup index between migration rehearsal Runs only."""
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import subprocess


def main():
    if '10.30.0.174' not in subprocess.check_output(['hostname', '-I'], text=True).split():
        raise RuntimeError('Not the rehearsal Source')
    loader = SourceFileLoader('control383', '/run/yeodam-b383/b-integration376-control.py')
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    db = module.database()
    try:
        if db.identity('source')[2:] != ['0', '0'] or db.identity('target')[2:] != ['1', '1']:
            raise RuntimeError('Run has not restored Source and protected Target')
        if db.query('proxy', "SELECT DISTINCT default_hostgroup FROM runtime_mysql_users WHERE username='yeodam_app';") != '10':
            raise RuntimeError('ProxySQL is not on Source')
        columns = db.query('source', "SELECT GROUP_CONCAT(column_name ORDER BY seq_in_index) FROM information_schema.statistics WHERE table_schema='yeodam_v1_source' AND table_name='trips' AND index_name='IX_TRIPS_USER_NAME_ACTIVE';")
        changed = columns in ('', 'NULL')
        if changed:
            db.query('source', 'SET SESSION lock_wait_timeout=5; ALTER TABLE yeodam_v1_source.trips ADD INDEX IX_TRIPS_USER_NAME_ACTIVE(user_id,trip_name,deleted_at), ALGORITHM=INPLACE, LOCK=NONE;', timeout=180)
        elif columns != 'user_id,trip_name,deleted_at':
            raise RuntimeError('Existing index definition differs; preserve it')
        plan = db.query('source', "EXPLAIN SELECT trip_id FROM yeodam_v1_source.trips WHERE user_id=707 AND trip_name='b383-readonly-diagnostic' AND deleted_at IS NULL LIMIT 1;")
        print(json.dumps({'changed': changed, 'index': 'IX_TRIPS_USER_NAME_ACTIVE', 'explain': plan}))
    finally:
        db.close()


if __name__ == '__main__':
    main()
