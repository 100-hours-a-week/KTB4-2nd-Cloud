#!/usr/bin/env python3
"""Verify only the named V3 foundation, using isolated synthetic Kubernetes objects.

No kubeconfig updates, AWS mutations, production connections or implicit cleanup.
Failure leaves namespaced fixtures for diagnosis. `cleanup` checks ownership first.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

NAMESPACE = "v3-foundation-smoke"
STORAGE_CLASS = "v3-foundation-smoke-gp3"
OWNER = "481-foundation-smoke"
IMAGE = "public.ecr.aws/docker/library/busybox:1.37.0"


def command(args, *, stdin=None, allow_failure=False):
    result = subprocess.run(args, input=stdin, text=True, capture_output=True, timeout=660)
    if result.returncode and not allow_failure:
        # All kubectl commands here address reviewed infrastructure or synthetic
        # fixtures, never Secrets. Preserve the failing probe; hide AWS error bodies.
        if args[0] == "kubectl":
            raise RuntimeError(f"kubectl failed ({result.returncode}): {' '.join(args)}\n{result.stderr[-2000:]}")
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(args[:4])}")
    return result


def security():
    return {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]}}


def container(name, script):
    return {"name": name, "image": IMAGE, "command": ["sh", "-ec", script],
            "securityContext": security(),
            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                          "limits": {"memory": "64Mi"}}}


def pod_spec(containers):
    return {"automountServiceAccountToken": False,
            "nodeSelector": {"yeodam.io/role": "system"},
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1000,
                                "runAsGroup": 1000, "fsGroup": 1000,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": containers}


def objects():
    server = container("http", "echo foundation-ok > /data/index.html; exec httpd -f -p 8080 -h /data")
    server["volumeMounts"] = [{"name": "data", "mountPath": "/data"}]
    server["readinessProbe"] = {"httpGet": {"path": "/", "port": 8080}, "periodSeconds": 2}
    spec = pod_spec([server])
    spec["volumes"] = [{"name": "data", "emptyDir": {"sizeLimit": "16Mi"}}]
    spec["affinity"] = {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{
        "labelSelector": {"matchLabels": {"app": "foundation-http"}},
        "topologyKey": "kubernetes.io/hostname"}]}}
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE, "labels": {
            "yeodam.io/owner": OWNER, "pod-security.kubernetes.io/enforce": "restricted"}}},
        {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {
            "name": STORAGE_CLASS, "labels": {"yeodam.io/owner": OWNER}},
            "provisioner": "ebs.csi.aws.com", "parameters": {"type": "gp3", "encrypted": "true"},
            "volumeBindingMode": "WaitForFirstConsumer", "reclaimPolicy": "Delete"},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "http", "namespace": NAMESPACE},
            "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "foundation-http"}},
                     "template": {"metadata": {"labels": {"app": "foundation-http"}}, "spec": spec}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "http", "namespace": NAMESPACE},
            "spec": {"selector": {"app": "foundation-http"}, "ports": [{"port": 8080, "targetPort": 8080}]}},
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "data", "namespace": NAMESPACE},
            "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": STORAGE_CLASS,
                     "resources": {"requests": {"storage": "1Gi"}}}},
    ]


def storage_job(name, script):
    item = container("storage", script)
    item["volumeMounts"] = [{"name": "data", "mountPath": "/data"}]
    spec = pod_spec([item])
    spec.update({"restartPolicy": "Never", "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]})
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": NAMESPACE},
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 300, "template": {"spec": spec}}}


def validate_target(account, cluster, context, kube_server, cluster_info):
    expected = f"arn:aws:eks:ap-northeast-2:{account}:cluster/{cluster}"
    if not cluster.startswith("yeodam-v3-") or context != expected:
        raise ValueError("Refusing a non-V3 or unexpected kubectl context")
    if cluster_info["arn"] != expected or kube_server != cluster_info["endpoint"]:
        raise ValueError("Kubeconfig server does not match the reviewed AWS cluster")


def select_nodes(nodes, groups=None, max_nodes=2, storage_group=None):
    system = [node for node in nodes if node["metadata"].get("labels", {}).get("yeodam.io/role") == "system"]
    if len(system) > max_nodes:
        raise ValueError("System node count exceeds the reviewed transition budget")
    if groups:
        if len(groups) != 2 or len(set(groups)) != 2:
            raise ValueError("Select exactly two distinct node groups")
        selected = [node for node in system if node["metadata"]["labels"].get("eks.amazonaws.com/nodegroup") in groups]
        if {node["metadata"]["labels"].get("eks.amazonaws.com/nodegroup") for node in selected} != set(groups):
            raise ValueError("Selected node group is missing")
    else:
        if max_nodes != 2 or storage_group:
            raise ValueError("Transition verification requires two explicit target groups")
        selected = system
    if storage_group and storage_group not in (groups or []):
        raise ValueError("Storage probe must target one of the selected groups")
    if len(selected) != 2 or len({node["metadata"]["labels"]["topology.kubernetes.io/zone"] for node in selected}) != 2:
        raise ValueError("Expected two selected system nodes in two AZs")
    for node in selected:
        conditions = {c["type"]: c["status"] for c in node["status"]["conditions"]}
        if conditions.get("Ready") != "True" or any(conditions.get(k) == "True" for k in ["MemoryPressure", "DiskPressure", "PIDPressure"]):
            raise ValueError("Selected node is not Ready or reports resource pressure")
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["render", "check", "cleanup"])
    parser.add_argument("--cluster", default="yeodam-v3-foundation")
    parser.add_argument("--account", default="483175530259")
    parser.add_argument("--profile", default="yeodam-admin")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--node-group", dest="node_groups", action="append")
    parser.add_argument("--max-system-nodes", type=int, choices=[2, 3], default=2)
    parser.add_argument("--storage-node-group")
    args = parser.parse_args()
    if args.mode == "render":
        print(json.dumps({"apiVersion": "v1", "kind": "List", "items": objects()}, indent=2))
        return
    if not os.environ.get("KUBECONFIG") or len(os.environ["KUBECONFIG"].split(os.pathsep)) != 1:
        raise ValueError("Set KUBECONFIG to one isolated V3 file; do not use the default user context")
    aws = ["aws", "--profile", args.profile, "--region", "ap-northeast-2"]
    identity = json.loads(command(aws + ["sts", "get-caller-identity"]).stdout)
    if identity["Account"] != args.account:
        raise ValueError("Unexpected AWS account")
    info = json.loads(command(aws + ["eks", "describe-cluster", "--name", args.cluster]).stdout)["cluster"]
    context = command(["kubectl", "config", "current-context"]).stdout.strip()
    kube_server = command(["kubectl", "config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"]).stdout
    validate_target(args.account, args.cluster, context, kube_server, info)
    # Long-running watches use their explicit --timeout, not a shorter HTTP timeout.
    kube = ["kubectl", "--context", context]

    def get(kind, name=None, namespace=None, missing_ok=False):
        cmd = kube + ["--request-timeout=30s"] + (["-n", namespace] if namespace else []) + ["get", kind]
        if name:
            cmd.append(name)
        if missing_ok:
            cmd.append("--ignore-not-found")
        output = command(cmd + ["-o", "json"]).stdout
        return json.loads(output) if output.strip() else None

    def owned(item):
        return item is not None and item["metadata"].get("labels", {}).get("yeodam.io/owner") == OWNER

    if args.mode == "cleanup":
        namespace = get("namespace", NAMESPACE, missing_ok=True)
        sc = get("storageclass", STORAGE_CLASS, missing_ok=True)
        if (namespace is not None and not owned(namespace)) or (sc is not None and not owned(sc)):
            raise ValueError("Refusing to clean objects not owned by the #481 smoke")
        # Record volume identity before namespace deletion, then wait for CSI cleanup.
        volume = get("pvc", "data", NAMESPACE, missing_ok=True) if namespace else None
        pv_name = volume.get("spec", {}).get("volumeName") if volume else None
        if namespace:
            command(kube + ["delete", "namespace", NAMESPACE, "--wait=true", "--timeout=180s"])
        if pv_name:
            command(kube + ["wait", "--for=delete", f"pv/{pv_name}", "--timeout=180s"])
        if sc:
            command(kube + ["delete", "storageclass", STORAGE_CLASS])
        print("Synthetic namespace, storage class and bound PV cleaned")
        return

    if args.output is None or args.output.exists():
        raise ValueError("Supply a new --output file for the verification receipt")
    if get("namespace", NAMESPACE, missing_ok=True) or get("storageclass", STORAGE_CLASS, missing_ok=True):
        raise ValueError("Existing smoke objects found; inspect and explicitly clean before rerunning")
    nodes = get("nodes")["items"]
    system = select_nodes(nodes, args.node_groups, args.max_system_nodes, args.storage_node_group)
    addons = {}
    for name in ["vpc-cni", "coredns", "kube-proxy", "eks-pod-identity-agent", "aws-ebs-csi-driver"]:
        addon = json.loads(command(aws + ["eks", "describe-addon", "--cluster-name", args.cluster, "--addon-name", name]).stdout)["addon"]
        if addon["status"] != "ACTIVE" or addon.get("health", {}).get("issues"):
            raise ValueError(f"Add-on unhealthy: {name}")
        addons[name] = addon["addonVersion"]

    # Managed add-on ACTIVE alone does not replace the in-cluster rollout check.
    for target in ["deployment/coredns", "daemonset/aws-node", "daemonset/kube-proxy"]:
        command(kube + ["-n", "kube-system", "rollout", "status", target, "--timeout=180s"])

    fixtures = objects()
    if args.node_groups:
        deployment = next(item for item in fixtures if item["kind"] == "Deployment")
        affinity = deployment["spec"]["template"]["spec"]["affinity"]
        affinity["nodeAffinity"] = {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{
            "matchExpressions": [{"key": "eks.amazonaws.com/nodegroup", "operator": "In", "values": args.node_groups}]
        }]}}
    command(kube + ["create", "-f", "-"], stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": fixtures}))
    command(kube + ["-n", NAMESPACE, "rollout", "status", "deployment/http", "--timeout=180s"])
    pods = get("pods", namespace=NAMESPACE)["items"]
    if len(pods) != 2 or len({pod["spec"]["nodeName"] for pod in pods}) != 2:
        raise ValueError("HTTP pods are not placed on distinct nodes")
    if {pod["spec"]["nodeName"] for pod in pods} != {node["metadata"]["name"] for node in system}:
        raise ValueError("HTTP probes must cover the two selected nodes")
    for pod in pods:
        peer = next(other for other in pods if other["metadata"]["name"] != pod["metadata"]["name"])
        command(kube + ["-n", NAMESPACE, "exec", pod["metadata"]["name"], "--", "sh", "-ec",
                       f"nslookup kubernetes.default.svc.cluster.local >/dev/null; "
                       f"test \"$(wget -T 5 -t 1 -qO- http://{peer['status']['podIP']}:8080/)\" = foundation-ok; "
                       "test \"$(wget -T 5 -t 1 -qO- http://http:8080/)\" = foundation-ok"])
    for name, script in [("write", "echo foundation-persisted > /data/receipt; sync"),
                         ("read", "test \"$(cat /data/receipt)\" = foundation-persisted")]:
        job = storage_job(name, script)
        if args.storage_node_group:
            job["spec"]["template"]["spec"]["nodeSelector"]["eks.amazonaws.com/nodegroup"] = args.storage_node_group
        command(kube + ["create", "-f", "-"], stdin=json.dumps(job))
        command(kube + ["-n", NAMESPACE, "wait", "--for=condition=complete", f"job/{name}", "--timeout=300s"])
        # Delete completed writer too, ensuring the second pod remounts the PVC.
        command(kube + ["-n", NAMESPACE, "delete", "job", name, "--wait=true"])
        if name == "write":
            command(kube + ["-n", NAMESPACE, "wait", "--for=delete", "pod", "-l", "job-name=write", "--timeout=120s"])
    pvc = get("pvc", "data", NAMESPACE)
    pv = get("pv", pvc["spec"]["volumeName"])
    volume_id = pv["spec"]["csi"]["volumeHandle"]
    volume = json.loads(command(aws + ["ec2", "describe-volumes", "--volume-ids", volume_id]).stdout)["Volumes"][0]
    if not volume["Encrypted"] or volume["VolumeType"] != "gp3":
        raise ValueError("PVC did not provision an encrypted gp3 EBS volume")
    receipt = {"account": args.account, "cluster": args.cluster, "addons": addons,
               "nodes": [node["metadata"]["name"] for node in system], "crossNodeHttp": True,
               "serviceDns": True, "pvcRemount": True, "encryptedGp3": True,
               "syntheticVolumeId": volume_id, "cleanupPending": True,
               "selectedNodeGroups": args.node_groups, "storageNodeGroup": args.storage_node_group,
               "nodeResources": [{"node": node["metadata"]["name"],
                                  "instanceType": node["metadata"]["labels"].get("node.kubernetes.io/instance-type"),
                                  "allocatable": node["status"].get("allocatable", {})} for node in system],
               "observedAtEpoch": int(time.time())}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(receipt, stream, indent=2)
    print("Node, add-on, cross-node HTTP, DNS and PVC checks passed; explicit cleanup remains")


if __name__ == "__main__":
    try:
        main()
    except subprocess.TimeoutExpired:
        print("Verification command timed out; inspect the retained synthetic objects", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
