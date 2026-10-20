#!/usr/bin/env python3
"""Ship the #370 host helper to the isolated V1 staging App and run one action."""

import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import UUID


ACCOUNT = "483175530259"
REGION = "ap-northeast-2"
INSTANCE = "i-05d320e3a87200d5a"
TARGET_INSTANCE = "i-05535ae6ff812972c"
REMOTE = "/run/yeodam-b2-pilot370-"


def aws(*arguments):
    result = subprocess.run(["aws", *arguments, "--profile", "yeodam-admin",
                             "--region", REGION, "--output", "json"],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("AWS call failed; check SSO, account, region, and network")
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start-source", "start-target", "start-failure",
                                            "stop-source", "stop-target", "stop-failure",
                                            "status", "pull-image", "db-status",
                                            "db-open", "db-close", "db-privileges",
                                            "request-direct", "request-queue", "request-retry",
                                            "request-auth", "request-ack-failure", "target-status", "target-schema",
                                            "target-audit"))
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if args.run_id:
        if str(UUID(args.run_id)) != args.run_id:
            raise ValueError("Canonical run UUID required")
    elif args.action.startswith(("start-", "request-")):
        raise ValueError("Run UUID required")
    if aws("sts", "get-caller-identity")["Account"] != ACCOUNT:
        raise RuntimeError("Wrong AWS account")
    database = args.action.startswith("db-")
    request_action = args.action.startswith("request-")
    target_action = args.action.startswith("target-")
    filename = ("b2-pilot370-db.py" if database else
                "b2-pilot370-request.py" if request_action else
                "b2-pilot370-target.py" if target_action else "b2-pilot370-host.py")
    source = Path(__file__).with_name(filename).read_bytes()
    remote = REMOTE + ("db.py" if database else "request.py" if request_action
                       else "target.py" if target_action else "host.py")
    action = args.action.split("-", 1)[1] if database or request_action or target_action else args.action
    instance = TARGET_INSTANCE if target_action else INSTANCE
    commands = [
        "set -eu",
        "umask 077",
        "printf '%s' '" + base64.b64encode(source).decode("ascii") + "' | base64 -d > " + remote,
        "chmod 0700 " + remote,
        "python3 " + remote + " " + action
        + (" --run-id " + args.run_id if args.run_id else ""),
    ]
    request = {"InstanceIds": [instance], "DocumentName": "AWS-RunShellScript",
               "Parameters": {"commands": commands}, "Comment": "Cloud #370 B2 isolated pilot"}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as payload:
        json.dump(request, payload)
        payload.flush()
        command_id = aws("ssm", "send-command", "--cli-input-json",
                         "file://" + payload.name)["Command"]["CommandId"]
    for _ in range(50):
        time.sleep(2)
        try:
            result = aws("ssm", "get-command-invocation", "--command-id", command_id,
                         "--instance-id", instance)
        except RuntimeError:
            continue
        if result["Status"] == "Success":
            print(result["StandardOutputContent"].strip())
            print("SSM command: " + command_id)
            return
        if result["Status"] in ("Failed", "Cancelled", "TimedOut"):
            # Pilot helpers print only status codes and identifiers, never credentials.
            print(result["StandardOutputContent"].strip())
            raise RuntimeError("SSM " + result["Status"] + " " + command_id + ": "
                               + result["StandardErrorContent"].strip()[:400])
    raise RuntimeError("SSM command did not finish in 100 seconds: " + command_id)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        raise SystemExit(1)
