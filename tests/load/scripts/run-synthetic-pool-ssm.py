#!/usr/bin/env python3
"""Install and run the synthetic pool tool on the V1 staging App through SSM."""

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
INSTANCE_NAME = "yeodam-v2-staging-v1-source-app"
REMOTE_SCRIPT = "/run/yeodam-synthetic-session-pool.py"
REMOTE_TRANSFER = "/run/yeodam-private-pool-transfer.py"
POOL_DIR = "/run/yeodam-synthetic-sessions"


def aws(profile, *arguments):
    result = subprocess.run(
        ["aws", *arguments, "--profile", profile, "--region", REGION, "--output", "json"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError("AWS command failed; check SSO login, region, and IAM permissions")
    return json.loads(result.stdout)


def canonical_uuid(value):
    if str(UUID(value)) != value:
        raise ValueError("run-id must be a canonical lowercase UUID")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "cleanup", "prepare-existing", "cleanup-existing"))
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--run-id", required=True, type=canonical_uuid)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--dataset-id", type=canonical_uuid)
    parser.add_argument("--profile", default="yeodam-admin")
    args = parser.parse_args()
    if not args.instance_id.startswith("i-") or not 1 <= args.count <= 1000:
        raise ValueError("Invalid instance ID or count")
    if args.action.endswith("existing") and not args.dataset_id:
        raise ValueError("Existing-user actions require --dataset-id")
    identity = aws(args.profile, "sts", "get-caller-identity")
    if identity["Account"] != ACCOUNT:
        raise RuntimeError("Refusing to use a different AWS account")
    reservations = aws(args.profile, "ec2", "describe-instances", "--instance-ids", args.instance_id)["Reservations"]
    instances = [instance for reservation in reservations for instance in reservation["Instances"]]
    if len(instances) != 1:
        raise RuntimeError("Expected exactly one V1 staging App instance")
    instance = instances[0]
    tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
    if tags.get("Name") != INSTANCE_NAME or instance["State"]["Name"] != "running":
        raise RuntimeError("Target is not the running V1 staging App")

    source = (Path(__file__).parent / "synthetic-session-pool.py").read_bytes()
    transfer = (Path(__file__).parent / "private-pool-transfer.py").read_bytes()
    output = f"{POOL_DIR}/{args.run_id}.json"
    commands = ["set -eu", "umask 077",
                "printf '%s' '" + base64.b64encode(source).decode("ascii") + "' | base64 -d > " + REMOTE_SCRIPT,
                "printf '%s' '" + base64.b64encode(transfer).decode("ascii") + "' | base64 -d > " + REMOTE_TRANSFER,
                "chmod 0700 " + REMOTE_SCRIPT + " " + REMOTE_TRANSFER]
    run = f"python3 {REMOTE_SCRIPT} {args.action} --run-id {args.run_id} --output {output}"
    if args.action in ("prepare", "prepare-existing"):
        run += f" --count {args.count}"
    if args.action.endswith("existing"):
        run += f" --dataset-id {args.dataset_id}"
    commands.append(run)
    request = {
        "InstanceIds": [args.instance_id], "DocumentName": "AWS-RunShellScript",
        "Parameters": {"commands": commands},
        "Comment": "#286 V1 staging existing-user sessions" if args.action.endswith("existing")
                   else "#195 V1 staging synthetic sessions",
    }
    with tempfile.NamedTemporaryFile(mode="w", prefix="yeodam-pool-ssm-", suffix=".json") as temporary:
        json.dump(request, temporary)
        temporary.flush()
        command_id = aws(args.profile, "ssm", "send-command", "--cli-input-json", "file://" + temporary.name)["Command"]["CommandId"]
    for _ in range(60):
        time.sleep(2)
        try:
            invocation = aws(args.profile, "ssm", "get-command-invocation",
                             "--command-id", command_id, "--instance-id", args.instance_id)
        except RuntimeError:
            continue  # SSM can return InvocationDoesNotExist immediately after dispatch.
        if invocation["Status"] == "Success":
            print(invocation["StandardOutputContent"].strip())
            print(f"SSM command: {command_id}")
            return
        if invocation["Status"] in ("Failed", "Cancelled", "TimedOut"):
            raise RuntimeError(f"SSM {invocation['Status']}: {invocation['StandardErrorContent'].strip()[:500]}")
    raise RuntimeError(f"SSM command {command_id} did not finish in 120 seconds")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
