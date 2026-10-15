#!/usr/bin/env python3
"""Materialize host-only runtime secrets without logging credentials."""
import json
import os
import pathlib
import re
import secrets
import subprocess
import tempfile

REGION = "ap-northeast-2"
BASE = pathlib.Path("/etc/yeodam/ai")


def aws(*args):
    result = subprocess.run(["aws", "--region", REGION, "--output", "json", *args], capture_output=True, text=True)
    if result.returncode:
        reason = re.sub(r"[0-9a-fA-F]{64}", "[REDACTED]", result.stderr.strip())
        raise RuntimeError("AWS runtime configuration failed: " + reason[:600])
    return json.loads(result.stdout)


def get_or_create(name):
    result = subprocess.run(["aws", "--region", REGION, "--output", "json", "ssm", "get-parameter", "--name", name, "--with-decryption"], capture_output=True, text=True)
    if result.returncode == 0:
        value = json.loads(result.stdout)["Parameter"]["Value"]
    elif "ParameterNotFound" in result.stderr:
        value = secrets.token_hex(32)
        with tempfile.NamedTemporaryFile(mode="w", dir="/run", prefix="ai-runtime-") as request:
            json.dump({"Name": name, "Type": "SecureString", "Value": value}, request)
            request.flush()
            aws("ssm", "put-parameter", "--cli-input-json", "file://" + request.name)
    else:
        raise RuntimeError("AI parameter read failed; credential was not changed")
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RuntimeError("AI credential must be 32 random bytes encoded as hex")
    return value


def env_file(name, values):
    path = BASE / name
    # Reject multiline values before writing a Docker env file.
    assert all("\n" not in str(v) and "\r" not in str(v) for v in values.values())
    with open(path, "w", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in values.items()))
    path.chmod(0o600)


def main():
    os.umask(0o077)
    embedding_key = get_or_create("/yeodam/v2/staging/ai/EMBEDDING_API_KEY")
    api_key = get_or_create("/yeodam/v2/staging/ai/API_KEY")
    redis_url = aws("ssm", "get-parameter", "--name", "/yeodam/v2/staging/ai/REDIS_URL", "--with-decryption")["Parameter"]["Value"]
    env_file("embedding.env", {"VLLM_API_KEY": embedding_key})
    values = {
        "API_KEY": api_key, "REDIS_URL": redis_url, "JOBS_ENV": "staging",
        "JOBS_PROCESS_HANDLER": "app.jobs.process_handler:run",
        "JOBS_STORY_HANDLER": "app.story.handler:run",
        "JOBS_FAKE_PIPELINE": "false", "FAKE_PIPELINE": "false",
        "IMAGE_SOURCE": "s3", "S3_BUCKET": os.environ["AI_APP_BUCKET"], "AWS_REGION": REGION,
        "EMBED_MODEL": "qwen3-vl-embedding-2b",
        "EMBEDDING_BASE_URL": "http://ai-gpu.staging.yeodam.internal:8000/v1",
        "EMBEDDING_SERVED_MODEL_NAME": "qwen3-vl-embedding-2b", "EMBEDDING_API_KEY": embedding_key,
        "QDRANT_URL": "http://ai-gpu.staging.yeodam.internal:6333",
        "KAFKA_BOOTSTRAP_SERVERS": "kafka.staging.yeodam.internal:9092",
        "KAFKA_SECURITY_PROTOCOL": "PLAINTEXT",
        "RELEASE": "caabb9ed7aafcad08c1f3d6087a6a93606a1f0d4",
    }
    for work in ("TRIP_CREATE", "TRIP_UPDATE", "STORY_CREATE"):
        for direction in ("REQUEST", "RESULT"):
            values[f"KAFKA_{work}_{direction}_TOPIC"] = f"staging.yeodam.{work.lower().replace('_', '-')}.{direction.lower()}"
    env_file("runner.env", values)
    credential = json.loads(aws("secretsmanager", "get-secret-value", "--secret-id", os.environ["AI_GHCR_SECRET"])["SecretString"])
    login = subprocess.run(["docker", "login", "ghcr.io", "--username", credential["username"], "--password-stdin"], input=credential["password"], text=True, capture_output=True)
    if login.returncode:
        raise RuntimeError("GHCR login failed; credential output suppressed")
    print("AI runtime configuration ready; credentials were not printed")


if __name__ == "__main__":
    main()
