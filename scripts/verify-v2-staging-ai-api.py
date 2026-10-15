#!/usr/bin/env python3
"""One-off Fargate dependency probe; run with the AI task role and backend SG.

Requires a Cloud-owned temporary S3 object. Never logs secrets or signed URLs.
Does not publish Kafka jobs or call VLM generation.
"""
import os
import sys

import boto3
import httpx
import redis


def request(label, url, *, headers=None):
    response = httpx.get(url, headers=headers, timeout=30)
    print(f"{label} status={response.status_code}", flush=True)
    response.raise_for_status()
    return response


def main():
    base = os.environ["AI_PROBE_BASE_URL"]
    health = request("INTERNAL_ALB_HEALTH", base + "/health").json()
    assert health["status"] == "ok"
    response = httpx.get(base + "/metrics", timeout=10)
    assert response.status_code == 401
    print("API_UNAUTHENTICATED_REJECTED", flush=True)
    request("API_AUTHENTICATED_METRICS", base + "/metrics", headers={"Authorization": "Bearer " + os.environ["API_KEY"]})
    assert redis.Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=5, socket_timeout=5).ping()
    print("REDIS_AUTH_OK", flush=True)
    request("QDRANT_READY", os.environ["QDRANT_URL"] + "/readyz")
    models = request("EMBEDDING_MODELS", os.environ["EMBEDDING_BASE_URL"] + "/models", headers={"Authorization": "Bearer " + os.environ["EMBEDDING_API_KEY"]}).json()
    assert any(m["id"] == os.environ["EMBEDDING_SERVED_MODEL_NAME"] for m in models["data"])
    models = request("RUNPOD_MODELS", os.environ["VLM_BASE_URL"] + "/models", headers={"Authorization": "Bearer " + os.environ["VLM_API_KEY"]}).json()
    assert any(m["id"] == os.environ["VLM_MODEL"] for m in models["data"])
    s3 = boto3.client("s3", region_name=os.environ["AWS_REGION"])
    url = s3.generate_presigned_url("get_object", Params={"Bucket": os.environ["S3_BUCKET"], "Key": "trip-uploads/cloud-221/derived/access-check"}, ExpiresIn=60)
    response = request("TASK_ROLE_S3_PRESIGNED_GET", url)
    assert response.content == b"cloud-221-check"
    print("AI_API_221_DEPENDENCIES_OK", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Exceptions may include a presigned URL or authorization details.
        print("AI_API_PROBE_FAILED type=" + type(exc).__name__, flush=True)
        sys.exit(1)
