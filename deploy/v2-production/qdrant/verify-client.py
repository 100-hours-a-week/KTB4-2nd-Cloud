"""Readiness and cross-environment port isolation; no application jobs or secrets."""
import json
import os
import socket
import urllib.request


def main():
    env = os.environ['PROBE_ENV']
    assert env in ['production', 'staging']
    host = 'ai-gpu.' + env + '.yeodam.internal'
    address = socket.gethostbyname(host)
    assert address == os.environ['EXPECTED_GPU_IP'], 'Wrong GPU private DNS destination'
    allowed, blocked = (6335, 6333) if env == 'production' else (6333, 6335)
    for port, path in [(8000, '/health'), (allowed, '/readyz')]:
        with urllib.request.urlopen(f'http://{host}:{port}{path}', timeout=10) as response:
            assert response.status == 200
    try:
        with socket.create_connection((address, blocked), timeout=5):
            raise RuntimeError('Unexpected cross-environment Qdrant connection')
    except (TimeoutError, socket.timeout):
        pass
    print(json.dumps({'environment': env, 'embeddingHealth': 200, 'qdrantHealth': 200,
                      'otherEnvironmentQdrantBlocked': True, 'passed': True}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit('Shared GPU client verification failed: ' + type(error).__name__) from None
