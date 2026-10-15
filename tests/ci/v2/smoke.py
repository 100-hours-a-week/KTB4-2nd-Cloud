"""Real V2 image journey with a deterministic HTTP embedding double, not GPU QA."""
import base64
import hashlib
import hmac
from http.cookies import SimpleCookie
import io
import json
import os
from pathlib import Path
import random
import time
from urllib.parse import urlsplit
import uuid

import httpx
from PIL import Image
import redis

ALLOWED_HOSTS = {'frontend', 'backend', 'ai-api', 'models', 's3', 'qdrant', 'connect'}
SID = '00000000-0000-4000-8000-000000000001'
BASE = 'http://backend:8080/api'
REPORT = {'runId': str(uuid.uuid4()), 'status': 'running', 'phase': 'starting',
          'realImages': True, 'realKafka': True, 'realCDC': True,
          'realAIHandler': True, 'realQdrant': True, 'realS3': False,
          'realGPU': False, 'browserE2E': False}


def token():
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).rstrip(b'=')
    now = int(time.time())
    body = encode({'alg': 'HS256', 'typ': 'JWT'}) + b'.' + encode({
        'iss': 'http://backend:8080', 'aud': ['yeodam-api'], 'sub': '900001',
        'sid': SID, 'iat': now, 'exp': now + 900,
    })
    signature = hmac.new(b'ci-only-secret-key-with-at-least-32-bytes', body, hashlib.sha256).digest()
    return (body + b'.' + base64.urlsafe_b64encode(signature).rstrip(b'=')).decode()


def picture(seed):
    rng = random.Random(seed)
    image = Image.frombytes('RGB', (96, 96), rng.randbytes(96 * 96 * 3))
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG', quality=95)
    return buffer.getvalue()  # Intentionally no GPS; expected unclassified photos.


def phase(name):
    REPORT['phase'] = name
    print(json.dumps({'runId': REPORT['runId'], 'phase': name}), flush=True)


def main():
    with httpx.Client(trust_env=False, timeout=20) as http:
        def request(method, url, expected=200, **kwargs):
            parsed = urlsplit(url)
            assert parsed.scheme == 'http' and parsed.hostname in ALLOWED_HOSTS, 'Non-CI URL rejected'
            response = http.request(method, url, **kwargs)
            if response.status_code != expected:
                # Exclude query strings, signed URLs and cookie values from evidence.
                raise AssertionError(f'{method} {parsed.hostname}{parsed.path}: HTTP {response.status_code}, expected {expected}')
            return response

        phase('health_and_auth')
        request('GET', 'http://frontend:3000/api/health')
        request('GET', BASE + '/actuator/health')
        request('GET', 'http://ai-api:8000/health')
        request('GET', BASE + '/users/me', expected=401)
        auth = redis.Redis(host='redis-auth', password='ci-auth-only', decode_responses=True)
        key = 'yeodam:ci:auth:session:' + SID
        auth.hset(key, mapping={'userId': '900001', 'refreshTokenHash': 'a' * 64,
                               'expiresAt': str(int(time.time() * 1000) + 900000)})
        auth.expire(key, 900)
        csrf = request('GET', BASE + '/auth/csrf')
        cookies = SimpleCookie()
        for value in csrf.headers.get_list('set-cookie'):
            cookies.load(value)
        headers = {'Cookie': 'accessToken=' + token() + '; CSRF_CONTEXT=' + cookies['CSRF_CONTEXT'].value,
                   'X-CSRF-TOKEN': csrf.json()['data']['token'], 'X-Request-ID': REPORT['runId']}
        request('GET', BASE + '/users/me', headers=headers)

        phase('create_trip')
        created = request('POST', BASE + '/trips', expected=201, headers=headers, json={
            'tripName': 'CI검증', 'startDate': '2026-10-01', 'endDate': '2026-10-02', 'regionCodes': ['50110'],
        }).json()
        trip = created['data']['tripId']
        REPORT['tripId'] = trip
        photos = [picture(239), picture(240)]
        phase('presigned_upload')
        issued = request('POST', BASE + f'/trips/{trip}/initial-attachments/upload-urls', headers=headers, json={
            'batchNo': 1, 'totalAttachmentCount': 2, 'complete': True,
            'attachments': [{'fileName': f'ci-{i}.jpg', 'contentType': 'image/jpeg', 'sizeBytes': len(p)}
                            for i, p in enumerate(photos)],
        }).json()['data']
        assert len(issued['attachments']) == 2
        for raw, target in zip(photos, issued['attachments'], strict=True):
            assert target['method'] == 'PUT'
            request('PUT', target['uploadUrl'], headers=target['headers'], content=raw)
        phase('accept_upload')
        request('POST', BASE + f'/trips/{trip}/initial-attachments', expected=202,
                headers=headers, json={'uploadId': issued['uploadId']})

        phase('wait_for_be_completion')
        observed = []
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            result = request('GET', BASE + f'/trips/{trip}/processing-status', headers=headers).json()['data']
            status = result['status']
            if not observed or observed[-1] != status:
                observed.append(status)
            if status == 'COMPLETED':
                break
            assert status in {'PROCESSING', 'FINALIZING'}, f'Unexpected terminal status: {status}'
            time.sleep(1)
        else:
            raise TimeoutError('BE did not reach COMPLETED')
        REPORT['observedStatuses'] = observed
        assert result['error'] is None and result['result'] is not None
        detail = request('GET', BASE + f'/trips/{trip}', headers=headers).json()['data']
        assert detail['tripId'] == trip and detail['attachmentCount'] == 0
        stats = request('GET', 'http://models:8000/stats').json()
        assert stats.get('image_embeddings') == 2 and stats.get('unexpected_requests', 0) == 0, stats
        REPORT['modelCalls'] = stats
        collections = request('GET', 'http://qdrant:6333/collections').json()['result']['collections']
        assert collections, 'No vectors persisted'
        counts = [request('GET', 'http://qdrant:6333/collections/' + c['name']).json()['result']['points_count']
                  for c in collections]
        assert sum(counts) == 2, counts
        REPORT['qdrantPoints'] = sum(counts)
        REPORT['status'] = 'passed'
        phase('completed')


if __name__ == '__main__':
    directory = Path(os.environ['RESULTS_DIR'])
    try:
        main()
    except Exception as error:
        REPORT.update(status='failed', errorType=type(error).__name__)
        print('V2_CI_FAILED phase=' + REPORT['phase'] + ' type=' + type(error).__name__, flush=True)
        raise
    finally:
        (directory / 'journey.json').write_text(json.dumps(REPORT, ensure_ascii=False, indent=2))
