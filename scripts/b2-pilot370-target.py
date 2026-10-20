#!/usr/bin/env python3
"""Apply the #333 Journal DDL and audit one B2 request on the isolated Target."""

import argparse
import json
import subprocess
import sys
from uuid import UUID


RUN = "4e05885f-483a-478f-bec9-7a54e2f6ae9d"
TRIP = "b37q4e058"
DDL = """CREATE TABLE yeodam_v1_source.migration_trip_request_result (
 request_id VARCHAR(36) NOT NULL PRIMARY KEY,
 user_id BIGINT NOT NULL,
 payload_hash CHAR(64) NOT NULL,
 status VARCHAR(16) NOT NULL,
 trip_id BIGINT NULL,
 error_code VARCHAR(64) NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
 INDEX idx_migration_trip_result_user (user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"""


def query(sql):
    command = ["docker", "exec", "-i", "yeodam-v2-mysql", "sh", "-ec",
               'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; '
               'exec mysql -uroot --batch --skip-column-names yeodam_v1_source']
    result = subprocess.run(command, input=sql, text=True, capture_output=True, check=False)
    if result.returncode:
        # Do not print command output or credentials in a failed SSM invocation.
        raise RuntimeError("Target root SQL failed; inspect exact schema/privilege state")
    return result.stdout.strip()


def state():
    identity = query("SELECT @@server_id,@@read_only,@@super_read_only;").split("\t")
    if identity[0] != "2":
        raise RuntimeError("Expected isolated Target server ID 2")
    table = int(query("SELECT COUNT(*) FROM information_schema.tables "
                      "WHERE table_schema='yeodam_v1_source' "
                      "AND table_name='migration_trip_request_result';"))
    return {"serverId": identity[0], "flags": identity[1:], "journalTable": table}


def audit():
    before = state()
    if before["journalTable"] != 1:
        raise RuntimeError("B2 Journal does not exist")
    journal = query("SELECT request_id,user_id,status,COALESCE(trip_id,0),"
                    "COALESCE(error_code,'') FROM migration_trip_request_result "
                    "WHERE request_id='" + RUN + "';")
    trips = query("SELECT trip_id,user_id,trip_name FROM trips "
                  "WHERE trip_name='" + TRIP + "';")
    regions = query("SELECT COUNT(*) FROM trip_regions WHERE trip_id IN "
                    "(SELECT trip_id FROM trips WHERE trip_name='" + TRIP + "');")
    print(json.dumps({"target": before, "journalRows": len(journal.splitlines()) if journal else 0,
                      "journal": journal.split("\t") if journal else None,
                      "tripRows": len(trips.splitlines()) if trips else 0,
                      "trip": trips.split("\t") if trips else None,
                      "regionRows": int(regions)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "schema", "audit"))
    args = parser.parse_args()
    assert str(UUID(RUN)) == RUN
    before = state()
    if args.action == "status":
        print(json.dumps(before))
    elif args.action == "schema":
        if before != {"serverId": "2", "flags": ["0", "0"], "journalTable": 0}:
            raise RuntimeError("Target must be isolated and writable with no Journal")
        query(DDL)
        print(json.dumps({"action": "schema", "before": before, "after": state()}))
    else:
        audit()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        raise SystemExit(1)
