#!/usr/bin/env python3
"""Issue one direct and two duplicate B2 requests using a private Source session."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID


DATASET = "c0014ce1-0398-4a0c-95de-f502cb2e412e"


def post(url, session, name, request_id=None):
    headers = {"Content-Type": "application/json", "X-CSRF-TOKEN": session["csrfToken"],
               "Cookie": "accessToken=" + session["accessToken"]
               + "; CSRF_CONTEXT=" + session["csrfContext"]}
    if request_id:
        headers["X-Request-ID"] = request_id
    payload = json.dumps({"tripName": name, "startDate": "2025-01-01",
                          "endDate": "2025-01-03", "regionCodes": ["50110"]}).encode()
    request = Request(url + "/api/trips", data=payload, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=10) as response:
            body = json.load(response)
            return response.status, body.get("data", {})
    except HTTPError as error:
        return error.code, {}


def count(name):
    result = subprocess.run(["mysql", "--protocol=socket", "-uroot", "-N", "-B",
                             "-Dyeodam_v1_source", "-e",
                             "SELECT COUNT(*) FROM trips WHERE trip_name='" + name + "';"],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("Source trip count failed")
    return int(result.stdout.strip())


def auth_check(session):
    headers = {"Cookie": "accessToken=" + session["accessToken"]
               + "; CSRF_CONTEXT=" + session["csrfContext"]}
    request = Request("http://127.0.0.1:18080/api/users/me", headers=headers)
    try:
        with urlopen(request, timeout=10) as response:
            status = response.status
    except HTTPError as error:
        status = error.code
    print(json.dumps({"action": "auth", "status": status}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("direct", "queue", "retry", "auth", "ack-failure"))
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if str(UUID(args.run_id)) != args.run_id:
        raise ValueError("Canonical run UUID required")
    path = Path("/run/yeodam-synthetic-sessions") / (args.run_id + ".json")
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise RuntimeError("Private session pool is missing or has unsafe permissions")
    pool = json.loads(path.read_text())
    if (pool.get("runId"), pool.get("datasetId"), pool.get("mode")) != (args.run_id, DATASET, "existing"):
        raise RuntimeError("Session pool identity mismatch")
    session = pool["sessions"][0]
    if args.action == "auth":
        auth_check(session)
        return
    direct_name = "b37d" + args.run_id[:5]
    queue_name = "b37q" + args.run_id[:5]
    if args.action == "direct":
        status, data = post("https://v1-staging.yeodam-2gether.com", session, direct_name)
        print(json.dumps({"action": "direct", "status": status, "tripName": direct_name,
                          "tripId": data.get("tripId"), "sourceRows": count(direct_name)}))
        if status != 201 or not isinstance(data.get("tripId"), int):
            raise RuntimeError("Direct 201 contract failed")
    else:
        failure = args.action == "ack-failure"
        url = "http://127.0.0.1:18082" if failure else "http://127.0.0.1:18080"
        status, data = post(url, session, queue_name, args.run_id)
        print(json.dumps({"action": args.action, "status": status, "tripName": queue_name,
                          "requestId": data.get("requestId"), "sourceRows": count(queue_name)}))
        if failure and status == 503 and count(queue_name) == 0:
            return
        if status != 202 or data.get("requestId") != args.run_id or count(queue_name) != 0:
            raise RuntimeError("B2 202 without Source DB write contract failed")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, KeyError, IndexError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        raise SystemExit(1)
