#!/usr/bin/env python3
"""One-off production Fargate probe. No inference, Kafka jobs, or existing photo writes."""
import json
import os
import socket
import sys
import uuid

import boto3
import httpx
import redis


def request(label, url, headers=None):
    response = httpx.get(url, headers=headers, timeout=25)
    print(f'{label} status={response.status_code}', flush=True)
    response.raise_for_status()
    return response


def main():
    base = 'http://ai-api.production.yeodam.internal'
    if os.environ.get('PROBE_MODE') == 'deny':
        # Resolve first: NXDOMAIN or connection refused is not SG denial evidence.
        addresses = socket.getaddrinfo('ai-api.production.yeodam.internal', 80,
                                       type=socket.SOCK_STREAM)
        for _, _, _, _, address in addresses:
            try:
                with socket.create_connection(address, timeout=5):
                    raise AssertionError('Unauthorized source reached internal ALB')
            except TimeoutError:
                continue
        print('UNAUTHORIZED_ALB_TCP_TIMEOUT', flush=True)
        return
    health = request('INTERNAL_ALB_HEALTH', base + '/health').json()
    assert health['status'] == 'ok'
    response = httpx.get(base + '/metrics', timeout=10)
    assert response.status_code == 401
    print('API_UNAUTHENTICATED_REJECTED', flush=True)
    request('AUTHENTICATED_METRICS', base + '/metrics',
            {'Authorization': 'Bearer ' + os.environ['API_KEY']})
    client = redis.Redis.from_url(os.environ['REDIS_URL'], socket_connect_timeout=5,
                                  socket_timeout=5)
    assert client.ping()
    print('PRODUCTION_REDIS_AUTH_OK', flush=True)
    request('PRODUCTION_QDRANT_READY', os.environ['QDRANT_URL'] + '/readyz')
    for label, prefix, expected in (
        ('EMBEDDING_MODELS', 'EMBEDDING', os.environ['EMBEDDING_SERVED_MODEL_NAME']),
        ('RUNPOD_MODELS', 'VLM', os.environ['VLM_MODEL']),
    ):
        models = request(label, os.environ[prefix + '_BASE_URL'] + '/models',
                         {'Authorization': 'Bearer ' + os.environ[prefix + '_API_KEY']}).json()
        assert any(model['id'] == expected for model in models['data'])
    run_id = str(uuid.UUID(os.environ['PROBE_RUN_ID']))
    key = f'trip-uploads/cloud-preflight-347-{run_id}/derived/read-probe.txt'
    s3 = boto3.client('s3', region_name='ap-northeast-2')
    assert os.environ['S3_BUCKET'] == 'yeodam-v1-app-data-483175530259-ap-northeast-2'
    url = s3.generate_presigned_url('get_object', Params={'Bucket': os.environ['S3_BUCKET'],
                                                       'Key': key}, ExpiresIn=60)
    response = request('TASK_ROLE_S3_PRESIGNED_GET', url)
    assert response.content == b'cloud-347-read-probe'
    print(json.dumps({'result': 'PRODUCTION_AI_DEPENDENCIES_OK', 'runId': run_id}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Do not print exceptions: HTTP errors can contain credentials or signed URLs.
        print('PRODUCTION_AI_PROBE_FAILED type=' + type(exc).__name__, flush=True)
        sys.exit(1)
