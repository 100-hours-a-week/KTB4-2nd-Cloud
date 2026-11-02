#!/usr/bin/env python3
"""Read a private Terraform plan JSON and reject operations outside #484's step.

Does not apply plans or print their contents. Resource actions and topology only.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

CLUSTER = "yeodam-v3-foundation"
PREFIX = 'aws_eks_node_group.system["'
A, C = "ap-northeast-2a", "ap-northeast-2c"
NEW_A, NEW_C = A + "-t3a-medium", C + "-t3a-medium"
STEPS = {
    "add-a": ({A, C, NEW_A}, NEW_A, "create"),
    "remove-a": ({C, NEW_A}, A, "delete"),
    "add-c": ({C, NEW_A, NEW_C}, NEW_C, "create"),
    "remove-c": ({NEW_A, NEW_C}, C, "delete"),
    "final": ({NEW_A, NEW_C}, None, None),
}


def check_receipts(step, verification, cleanup):
    if step not in {"remove-a", "remove-c"}:
        return
    candidate = "system-" + (NEW_A if step == "remove-a" else NEW_C)
    if not verification or not cleanup:
        raise ValueError("Removing a legacy group requires candidate verification and cleanup receipts")
    if verification.get("cluster") != CLUSTER or verification.get("account") != "483175530259":
        raise ValueError("Verification receipt targets another environment")
    if verification.get("storageNodeGroup") != candidate or candidate not in verification.get("selectedNodeGroups", []):
        raise ValueError("Verification did not exercise this candidate group")
    if not all(verification.get(key) is True for key in ["crossNodeHttp", "serviceDns", "pvcRemount", "encryptedGp3"]):
        raise ValueError("Candidate verification is incomplete")
    if (cleanup.get("cleanupComplete") is not True or cleanup.get("ebsDeletedConfirmed") is not True
            or not verification.get("syntheticVolumeId")
            or cleanup.get("syntheticVolumeId") != verification.get("syntheticVolumeId")):
        raise ValueError("Synthetic resource cleanup is not confirmed")


def inspect_plan(plan, step, verification=None, cleanup=None):
    check_receipts(step, verification, cleanup)
    if plan.get("errored"):
        raise ValueError("Terraform reported a plan error")
    variables = {key: value["value"] for key, value in plan["variables"].items()}
    if variables.get("expected_account_id") != "483175530259" or variables.get("aws_region") != "ap-northeast-2" or variables.get("cluster_name") != CLUSTER:
        raise ValueError("Plan target does not match the reviewed account/region/cluster")
    topology, target, action = STEPS[step]
    groups = variables["system_node_groups"]
    if set(groups) != topology:
        raise ValueError("Plan topology does not match this migration step")
    for key, group in groups.items():
        az = A if key.startswith(A) else C
        if group["availability_zone"] != az:
            raise ValueError("Unexpected AZ assignment")
        if key in {A, C}:
            if group["instance_type"] != "m6i.large" or group.get("launch_template_version") != "1":
                raise ValueError("Legacy groups must retain m6i and their original LT v1")
        elif group["instance_type"] != "t3a.medium":
            raise ValueError("Candidate size changed")
    changes = [item for item in plan.get("resource_changes", []) if item["mode"] == "managed" and item["change"]["actions"] != ["no-op"]]
    expected_address = PREFIX + target + '\"]' if target else None
    node_changes = []
    template_updates = 0
    for item in changes:
        address, change = item["address"], item["change"]
        if address == expected_address and change["actions"] == [action]:
            value = change["after"] if action == "create" else change["before"]
            expected_type = "t3a.medium" if action == "create" else "m6i.large"
            if value["cluster_name"] != CLUSTER or value["node_group_name"] != "system-" + target or value["instance_types"] != [expected_type]:
                raise ValueError("Unexpected node group mutation")
            if value["scaling_config"] != [{"desired_size": 1, "min_size": 1, "max_size": 1}]:
                raise ValueError("Unexpected node count")
            node_changes.append(item)
        elif step == "add-a" and address == "aws_launch_template.system" and change["actions"] == ["update"]:
            before, after = change["before"], change["after"]
            if before.get("credit_specification") not in ([], None) or after.get("credit_specification") != [{"cpu_credits": "unlimited"}]:
                raise ValueError("Unexpected credit configuration")
            computed = {key for key, value in change.get("after_unknown", {}).items() if value is True}
            for key in set(before) | set(after):
                if key not in computed | {"credit_specification", "latest_version", "default_version"} and before.get(key) != after.get(key):
                    raise ValueError("Launch template change exceeds credit settings")
            template_updates += 1
        else:
            raise ValueError(f"Out-of-scope resource action: {address} {change['actions']}")
    if len(node_changes) != (0 if step == "final" else 1):
        raise ValueError("Expected exactly one node-group action per transition")
    if step == "add-a" and template_updates != 1:
        raise ValueError("First step must explicitly pin the new credit configuration")
    return {"step": step, "account": "483175530259", "region": "ap-northeast-2", "cluster": CLUSTER,
            "create": sum(item["change"]["actions"] == ["create"] for item in changes),
            "update": sum(item["change"]["actions"] == ["update"] for item in changes),
            "destroy": sum(item["change"]["actions"] == ["delete"] for item in changes),
            "plannedGroups": sorted(groups), "plannedSteadyNodes": len(groups), "scopePassed": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan_json", type=Path)
    parser.add_argument("--step", choices=STEPS, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verification", type=Path)
    parser.add_argument("--cleanup", type=Path)
    args = parser.parse_args()
    verification = json.loads(args.verification.read_text()) if args.verification else None
    cleanup = json.loads(args.cleanup.read_text()) if args.cleanup else None
    result = inspect_plan(json.loads(args.plan_json.read_text()), args.step, verification, cleanup)
    result["planJsonSha256"] = hashlib.sha256(args.plan_json.read_bytes()).hexdigest()
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        with args.output.open("x") as stream:
            stream.write(text)
    print(text, end="")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
