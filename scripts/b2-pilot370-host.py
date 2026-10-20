#!/usr/bin/env python3
"""Run isolated #370 BE containers on the migration Source host.

The Docker API copies the current staging BE environment inside the host. It
never writes credentials to a shell command, SSM output, or a temporary file.
"""

import argparse
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
from urllib.parse import quote


SOCKET = "/var/run/docker.sock"
SOURCE = "yeodam-app-backend-1"
IMAGE = (
    "ghcr.io/100-hours-a-week/yeodam-backend@"
    "sha256:ba8db015a928afd8eb32626e9765d20152fc60d65a5430cca844c09bf0a7b0d5"
)
TOPIC_PREFIX = "staging.yeodam.migration.b2.370."
CONTAINERS = {"source": ("yeodam-b2-370-source", "18080"),
              "target": ("yeodam-b2-370-target", "18081"),
              "failure": ("yeodam-b2-370-failure", "18082")}
PARAMETER_PREFIX = "/yeodam/v2/staging/v1-source/deploy/"


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(SOCKET)


def docker(method, path, payload=None, allowed=(200, 201, 204)):
    connection = UnixConnection("localhost", timeout=15)
    body = None if payload is None else json.dumps(payload).encode()
    connection.request(method, path, body=body,
                       headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    data = response.read()
    status = response.status
    connection.close()
    if status not in allowed:
        # Docker error bodies can include configuration. Do not print them.
        raise RuntimeError(f"Docker {method} {path.split('?')[0]} returned HTTP {status}")
    return json.loads(data) if data else {}


def inspect(name):
    return docker("GET", "/containers/" + quote(name, safe="") + "/json",
                  allowed=(200,))


def ensure_host():
    addresses = subprocess.check_output(["hostname", "-I"], text=True).split()
    if "10.30.0.174" not in addresses:
        raise RuntimeError("Expected migration Source EC2 only")


def existing(name):
    try:
        return inspect(name)
    except RuntimeError as error:
        if "HTTP 404" in str(error):
            return None
        raise


def start(role, run_id):
    name, port = CONTAINERS[role]
    if existing(name):
        raise RuntimeError("Run container already exists; inspect it before retrying")
    base = inspect(SOURCE)
    if not base["State"]["Running"] or base["HostConfig"]["NetworkMode"] != "yeodam-app-network":
        raise RuntimeError("Unexpected staging BE state or network")
    if "migration-probe-v1-sha-816d69ba" not in base["Config"]["Image"]:
        raise RuntimeError("Unexpected original V1 rehearsal BE image")
    docker("GET", "/images/" + quote(IMAGE, safe="") + "/json", allowed=(200,))
    env = dict(item.split("=", 1) for item in base["Config"]["Env"] if "=" in item)
    if not env.get("MYSQL_URL", "").startswith("jdbc:mysql://proxysql:6033/yeodam_v1_source"):
        raise RuntimeError("Original BE is not using the migration ProxySQL schema")
    env.update({
        "MIGRATION_KAFKA_BOOTSTRAP_SERVERS": "10.30.10.95:9092",
        "MIGRATION_TRIP_TOPIC": TOPIC_PREFIX + run_id + ".requests",
        "MIGRATION_TRIP_CONSUMER_ENABLED": "true" if role == "target" else "false",
        "MIGRATION_TRIP_CONSUMER_GROUP": "yeodam-b2-370-" + run_id,
        "SPRING_FLYWAY_ENABLED": "false",  # Existing V1 rehearsal schema is already versioned.
    })
    if role == "failure":
        env["MIGRATION_KAFKA_BOOTSTRAP_SERVERS"] = "127.0.0.1:1"
    if role == "target":
        env["MYSQL_URL"] = "jdbc:mysql://10.30.10.28:3306/yeodam_v1_source"
        env["MIGRATION_TRIP_MODE_FILE"] = ""
    else:
        mode = Path("/run/yeodam-b2-370" + ("-failure" if role == "failure" else "")) / "mode"
        if mode.exists():
            raise RuntimeError("Pilot mode file already exists; inspect it first")
        # The marker has no credential; the non-root BE must traverse/read it.
        mode.parent.mkdir(mode=0o755)
        descriptor = os.open(mode, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "w") as stream:
            stream.write("queue\n")
        env["MIGRATION_TRIP_MODE_FILE"] = str(mode)
    host_config = {
        "NetworkMode": "yeodam-app-network",
        "PortBindings": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": port}]},
        "RestartPolicy": {"Name": "no"},
    }
    if role != "target":
        host_config["Binds"] = [str(mode) + ":" + str(mode) + ":ro"]
    payload = {
        "Image": IMAGE,
        "Env": [key + "=" + value for key, value in env.items()],
        "Labels": {"yeodam.b2.pilot370": "true", "yeodam.b2.role": role,
                   "yeodam.b2.run": run_id},
        "ExposedPorts": {"8080/tcp": {}},
        "HostConfig": host_config,
    }
    docker("POST", "/containers/create?name=" + quote(name, safe=""), payload, allowed=(201,))
    docker("POST", "/containers/" + name + "/start", allowed=(204,))
    print(json.dumps({"role": role, "container": name, "loopbackPort": int(port),
                      "runId": run_id, "status": "started"}))


def stop(role):
    name, _ = CONTAINERS[role]
    container = existing(name)
    if not container:
        print(json.dumps({"role": role, "status": "absent"}))
        return
    if container["Config"]["Labels"].get("yeodam.b2.pilot370") != "true":
        raise RuntimeError("Refusing to remove a container without the pilot label")
    if container["State"]["Running"]:
        docker("POST", "/containers/" + name + "/stop?t=30", allowed=(204,))
    docker("DELETE", "/containers/" + name, allowed=(204,))
    if role != "target":
        mode = Path("/run/yeodam-b2-370" + ("-failure" if role == "failure" else "")) / "mode"
        if mode.is_file() and mode.read_text() == "queue\n":
            mode.unlink()
            mode.parent.rmdir()
    print(json.dumps({"role": role, "status": "removed"}))


def status():
    for role, (name, port) in CONTAINERS.items():
        container = existing(name)
        state = "absent" if container is None else container["State"]["Status"]
        print(json.dumps({"role": role, "container": name, "loopbackPort": int(port),
                          "status": state}))


def pull_image():
    def parameter(name, secret=False):
        cmd = ["aws", "ssm", "get-parameter", "--region", "ap-northeast-2",
               "--name", PARAMETER_PREFIX + name, "--query", "Parameter.Value",
               "--output", "text", "--no-cli-pager"]
        if secret:
            cmd.append("--with-decryption")
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode or not result.stdout.strip():
            raise RuntimeError("Staging GHCR credential lookup failed")
        return result.stdout.strip()

    username = parameter("GHCR_USERNAME")
    token = parameter("GHCR_TOKEN", secret=True)
    with tempfile.TemporaryDirectory(prefix="b2-370-ghcr-", dir="/run") as config:
        login = subprocess.run(["docker", "--config", config, "login", "ghcr.io",
                                "--username", username, "--password-stdin"],
                               input=token, text=True, capture_output=True, check=False)
        token = None
        if login.returncode:
            raise RuntimeError("GHCR login failed")
        pull = subprocess.run(["docker", "--config", config, "pull", IMAGE],
                              capture_output=True, check=False)
        if pull.returncode:
            raise RuntimeError("Pinned rehearsal image pull failed")
    metadata = docker("GET", "/images/" + quote(IMAGE, safe="") + "/json", allowed=(200,))
    if metadata.get("Architecture") != "amd64" or metadata.get("Os") != "linux":
        raise RuntimeError("Unexpected image platform")
    print(json.dumps({"status": "pulled", "image": IMAGE, "platform": "linux/amd64"}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start-source", "start-target", "start-failure",
                                            "stop-source", "stop-target", "stop-failure",
                                            "status", "pull-image"))
    parser.add_argument("--run-id")
    args = parser.parse_args()
    ensure_host()
    if args.action.startswith("start"):
        if not args.run_id or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", args.run_id):
            raise ValueError("Canonical run UUID required")
        start(args.action.split("-", 1)[1], args.run_id)
    elif args.action.startswith("stop"):
        stop(args.action.split("-", 1)[1])
    elif args.action == "pull-image":
        pull_image()
    else:
        status()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        raise SystemExit(1)
