#!/usr/bin/env python3
"""One-off Fargate probe using BE roles/SG and AI's existing Python image.

Keep production Host/SNI while connecting to the candidate ALB. Never follows
OAuth redirects, submits jobs, or prints cookies, keys, JWTs or signed URLs.
"""
import asyncio
import hashlib
import http.client
from http.cookies import SimpleCookie
import json
import os
import socket
import ssl
import sys
from urllib.parse import parse_qs, urlsplit
import uuid

import boto3
import httpx
import redis
from aiokafka import AIOKafkaConsumer


class PreviewConnection(http.client.HTTPSConnection):
    def connect(self):
        raw = socket.create_connection((os.environ['PROBE_ALB_DNS'], 443), timeout=20)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname='yeodam-2gether.com')


def preview(label, path, expected, *, method='GET', headers=None):
    connection = PreviewConnection('yeodam-2gether.com', timeout=25)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        print(f'{label} status={response.status}', flush=True)
        assert response.status in expected
        return dict(response.getheaders()), body
    finally:
        connection.close()


async def kafka_metadata():
    consumer = AIOKafkaConsumer(bootstrap_servers=os.environ['KAFKA_BOOTSTRAP_SERVERS'],
                               group_id=None, enable_auto_commit=False,
                               request_timeout_ms=10000)
    try:
        await consumer.start()
        topics = await consumer.topics()
        expected = {value for key, value in os.environ.items()
                    if key.startswith('KAFKA_') and key.endswith('_TOPIC')}
        assert len(expected) == 6 and expected.issubset(topics)
        print('PRODUCTION_KAFKA_SIX_TOPICS_OK', flush=True)
    finally:
        await consumer.stop()


def main():
    _, body = preview('ALB_BACKEND_HEALTH', '/api/actuator/health', {200})
    assert json.loads(body)['status'] == 'UP'
    preview('UNAUTHENTICATED_USER_REJECTED', '/api/users/me', {401})
    _, metrics_body = preview('PUBLIC_METRICS_BLOCKED', '/api/actuator/prometheus', {401, 403})
    assert b'# HELP' not in metrics_body
    headers, _ = preview('SAME_ORIGIN_OPTIONS', '/api/trips', {200, 204}, method='OPTIONS', headers={
        'Origin': 'https://yeodam-2gether.com',
        'Access-Control-Request-Method': 'POST',
        'Access-Control-Request-Headers': 'content-type,x-csrf-token',
    })
    allow_origin = {k.lower(): v for k, v in headers.items()}.get('access-control-allow-origin')
    assert allow_origin in (None, 'https://yeodam-2gether.com')
    preview('UNTRUSTED_ORIGIN_REJECTED', '/api/trips', {403}, method='OPTIONS', headers={
        'Origin': 'https://invalid.example',
        'Access-Control-Request-Method': 'POST',
        'Access-Control-Request-Headers': 'content-type,x-csrf-token',
    })
    client = redis.Redis(host=os.environ['REDIS_HOST'], port=int(os.environ['REDIS_PORT']),
                         password=os.environ['REDIS_PASSWORD'], ssl=True,
                         socket_connect_timeout=5, socket_timeout=5)
    assert client.ping()
    print('PRODUCTION_AUTH_REDIS_TLS_OK', flush=True)
    state_key = None
    try:
        headers, _ = preview('OAUTH_AUTHORIZE', '/api/auth/kakao/authorize', {302})
        headers = {k.lower(): v for k, v in headers.items()}
        destination = urlsplit(headers['location'])
        query = parse_qs(destination.query)
        state_key = os.environ['AUTH_SESSION_KEY_PREFIX'] + 'oauth-state:' + hashlib.sha256(query['state'][0].encode()).hexdigest()
        assert destination.scheme == 'https' and destination.netloc == 'kauth.kakao.com'
        assert query['redirect_uri'] == ['https://yeodam-2gether.com/api/auth/kakao/callback']
        assert query['client_id'] == [os.environ['KAKAO_CLIENT_ID']]
        cookies = SimpleCookie(); cookies.load(headers['set-cookie'])
        assert len(cookies) == 1
        cookie = next(iter(cookies.values()))
        assert cookie['secure'] and cookie['httponly'] and cookie['path'] == '/api/auth'
        assert client.get(state_key) == hashlib.sha256(cookie.value.encode()).hexdigest().encode()
        assert 0 < client.ttl(state_key) <= 300
        print('OAUTH_STATE_AND_COOKIE_OK', flush=True)
    finally:
        if state_key is not None:
            client.delete(state_key)
            assert not client.exists(state_key)
            print('OWN_OAUTH_STATE_REMOVED', flush=True)
        client.close()
    response = httpx.get(os.environ['AI_SERVER_BASE_URL'] + '/metrics',
                         headers={'Authorization': 'Bearer ' + os.environ['AI_SERVER_API_KEY']}, timeout=20)
    assert response.status_code == 200
    print('BACKEND_TO_AI_AUTHENTICATED_OK', flush=True)
    asyncio.run(kafka_metadata())
    run_id = str(uuid.UUID(os.environ['PROBE_RUN_ID']))
    s3 = boto3.client('s3', region_name='ap-northeast-2')
    assert os.environ['ATTACHMENT_S3_BUCKET'] == 'yeodam-v1-app-data-483175530259-ap-northeast-2'
    url = s3.generate_presigned_url('get_object', Params={
        'Bucket': os.environ['ATTACHMENT_S3_BUCKET'],
        'Key': f'trip-uploads/cloud-preflight-350-{run_id}/derived/read-probe.txt',
    }, ExpiresIn=60)
    response = httpx.get(url, timeout=20)
    assert response.status_code == 200 and response.content == b'cloud-350-read-probe'
    print('BACKEND_TASK_ROLE_S3_PRESIGNED_GET_OK', flush=True)
    print('PRODUCTION_BACKEND_PREFLIGHT_OK', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('PRODUCTION_BACKEND_PROBE_FAILED type=' + type(exc).__name__, flush=True)
        sys.exit(1)
