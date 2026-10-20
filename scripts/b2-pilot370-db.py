#!/usr/bin/env python3
"""Guard the isolated Target DB write window for Cloud #370's one-request pilot."""

import argparse
import importlib.util
from importlib.machinery import SourceFileLoader
import json
from pathlib import Path
import sys


CONTROLLER = Path("/usr/local/sbin/yeodam-staging-db-cutover")
TARGET = "10.30.10.28"
EMAIL = "mig-data-c0014ce103984a0c95def502cb2e412e-00000@staging.invalid"
QUEUED_TRIP = "b37q4e058"


def controller_module():
    loader = SourceFileLoader("cutover370", str(CONTROLLER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def state(db):
    source = db.identity("source")
    target = db.identity("target")
    replica = db.status()
    table = db.query("target", "SELECT COUNT(*) FROM information_schema.tables "
                     "WHERE table_schema='yeodam_v1_source' "
                     "AND table_name='migration_trip_request_result';")
    source_user = db.query("source", "SELECT user_id FROM yeodam_v1_source.users WHERE email='" + EMAIL + "';")
    target_user = db.query("target", "SELECT user_id FROM yeodam_v1_source.users WHERE email='" + EMAIL + "';")
    return {"sourceServerId": source[0], "sourceFlags": source[2:],
            "targetServerId": target[0], "targetFlags": target[2:],
            "replicaIO": replica["Replica_IO_Running"],
            "replicaSQL": replica["Replica_SQL_Running"],
            "replicaErrorIO": replica["Last_IO_Errno"],
            "replicaErrorSQL": replica["Last_SQL_Errno"],
            "journalTable": int(table),
            "targetTransactions": int(db.query("target", "SELECT COUNT(*) FROM information_schema.innodb_trx;")),
            "targetOtherClients": int(db.query("target", "SELECT COUNT(*) FROM information_schema.processlist "
                                               "WHERE USER NOT IN ('system user','event_scheduler','yeodam_cutover') "
                                               "AND ID!=CONNECTION_ID();")),
            "sourceUserId": source_user, "targetUserId": target_user,
            "sourceQueuedTripRows": int(db.query("source", "SELECT COUNT(*) FROM yeodam_v1_source.trips "
                                                 "WHERE trip_name='" + QUEUED_TRIP + "';")),
            "targetQueuedTripRows": int(db.query("target", "SELECT COUNT(*) FROM yeodam_v1_source.trips "
                                                 "WHERE trip_name='" + QUEUED_TRIP + "';")),
            "proxyDefaultHG": db.query("proxy", "SELECT DISTINCT default_hostgroup FROM runtime_mysql_users "
                                       "WHERE username='yeodam_app';"),
            "proxyActiveRules": int(db.query("proxy", "SELECT COUNT(*) FROM runtime_mysql_query_rules "
                                             "WHERE active=1;"))}


def guard(result, writable=False, allow_applied=False):
    if result["sourceServerId"] != "1" or result["sourceFlags"] != ["0", "0"]:
        raise RuntimeError("Unexpected Source Writer state")
    if result["targetServerId"] != "2":
        raise RuntimeError("Unexpected Target server")
    if result["replicaIO"] != "No" or result["replicaSQL"] != "No":
        raise RuntimeError("Target replication must be stopped for isolated pilot")
    if result["replicaErrorIO"] != "0" or result["replicaErrorSQL"] != "0":
        raise RuntimeError("Target replication reports an error")
    if result["targetFlags"] != (["0", "0"] if writable else ["1", "1"]):
        raise RuntimeError("Unexpected Target read_only state")
    if result["targetTransactions"] != 0:
        raise RuntimeError("Target has an active transaction")
    if result["targetOtherClients"] != 0:
        raise RuntimeError("Target has an unexpected client")
    if not result["sourceUserId"] or result["sourceUserId"] != result["targetUserId"]:
        raise RuntimeError("Preloaded Source/Target pilot user mismatch")
    if result["sourceQueuedTripRows"] != 0 or (result["targetQueuedTripRows"] != 0 and not allow_applied):
        raise RuntimeError("Pilot queued trip already exists")
    if result["proxyDefaultHG"] != "10" or result["proxyActiveRules"] != 0:
        raise RuntimeError("ProxySQL is not isolated to Source")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "privileges", "open", "close"))
    args = parser.parse_args()
    if not CONTROLLER.is_file():
        raise RuntimeError("Expected existing staging DB controller")
    module = controller_module()
    db = module.Database(TARGET)
    try:
        before = state(db)
        if args.action == "status":
            print(json.dumps(before))
            return
        if args.action == "privileges":
            grants = db.query("target", "SHOW GRANTS FOR CURRENT_USER;")
            print(json.dumps({"targetCutoverCreate": "ALL PRIVILEGES" in grants or
                              "CREATE" in grants, "grantLines": len(grants.splitlines())}))
            return
        if args.action == "open":
            guard(before)
            if before["journalTable"] != 0:
                raise RuntimeError("Pilot Journal table already exists; inspect it first")
            db.query("target", "SET PERSIST super_read_only=OFF; SET PERSIST read_only=OFF;")
        else:
            guard(before, writable=True, allow_applied=True)
            db.query("target", "SET PERSIST super_read_only=ON; SET PERSIST read_only=ON;")
        print(json.dumps({"action": args.action, "before": before, "after": state(db)}))
    finally:
        db.ca.unlink(missing_ok=True)
        db.ca.parent.rmdir()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        raise SystemExit(1)
