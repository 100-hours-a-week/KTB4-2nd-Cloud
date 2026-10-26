#!/usr/bin/env python3
"""Exercise the actual Nginx 1.24 policy in a disposable container with a local echo backend."""
from collections import Counter
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'monitoring/v1-staging'))
from nginx_create_dry_run import PREFIX, LOCATION
from nginx_read_enforcement import ENFORCED_PREFIX, ENFORCED_SERVER
from nginx_create_enforcement import ENFORCED_LOCATION


class Echo(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self):
        size = int(self.headers.get('Content-Length', '0'))
        value = {'method': self.command, 'uri': self.path, 'body': self.rfile.read(size).decode(),
                 'csrf': self.headers.get('X-CSRF-TOKEN'), 'cookie': self.headers.get('Cookie'),
                 'requestId': self.headers.get('X-Request-ID')}
        data = json.dumps(value).encode()
        self.send_response(201 if self.command == 'POST' else 200)
        self.send_header('X-Request-ID', self.headers.get('X-Request-ID', ''))
        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

    do_GET = do_POST = respond


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enforce', action='store_true')
    args = parser.parse_args()
    server = ThreadingHTTPServer(('0.0.0.0', 0), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    container = None
    try:
        with tempfile.TemporaryDirectory(prefix='yeodam430-') as tmp:
            root = Path(tmp); root.chmod(0o755)
            config = 'events {}\nhttp {\n'+ENFORCED_PREFIX+PREFIX+'''
log_format proof escape=json '{"method":"$request_method","uri":"$uri","status":$status,"limit":"$limit_req_status","upstream":"$upstream_status","header_time":"$upstream_header_time"}';
access_log /dev/stdout proof;
map $request_id $yeodam_staging_request_id { default $request_id; }
upstream yeodam_backend { server host.docker.internal:''' + str(server.server_port) + '''; }
server { listen 8080;
proxy_set_header X-Request-ID $request_id;
''' + ENFORCED_SERVER + (ENFORCED_LOCATION if args.enforce else LOCATION) + '''
location /api/trips { proxy_pass http://yeodam_backend; }
location / { proxy_pass http://yeodam_backend; }
} }
'''
            (root/'nginx.conf').write_text(config); (root/'nginx.conf').chmod(0o644)
            host = ['--add-host', 'host.docker.internal:host-gateway'] if sys.platform == 'linux' else []
            container = subprocess.check_output(['docker', 'run', '--rm', '-d', *host,
                '-p', '127.0.0.1::8080', '-v', str(root/'nginx.conf')+':/etc/nginx/nginx.conf:ro',
                'nginx:1.24.0-alpine'], text=True).strip()
            port = subprocess.check_output(['docker', 'port', container, '8080'], text=True).strip().split(':')[-1]
            origin = 'http://127.0.0.1:'+port

            def request(path, body=None):
                req = urllib.request.Request(origin+path, data=body,
                    headers={'X-CSRF-TOKEN': 'ci-synthetic', 'Cookie': 'accessToken=ci-synthetic'})
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        data = json.load(r)
                        assert r.headers.get_all('X-Request-ID') == [data['requestId']]
                        return r.status, data
                except urllib.error.HTTPError as r:
                    if args.enforce and body is not None and r.code == 429:
                        assert len(r.headers.get('X-Request-ID', '')) == 32
                    return r.code, None

            for attempt in range(20):
                try:
                    if request('/ready')[0] == 200:
                        break
                except OSError:
                    if attempt == 19:
                        raise
                    time.sleep(.1)
            body = b'{"tripName":"ci-only"}'
            posts = [request('/api/trips?proof=preserved', body) for _ in range(20)]
            assert all(data['method'] == 'POST' and data['body'] == body.decode()
                       and data['uri'] == '/api/trips?proof=preserved' and data['csrf'] == 'ci-synthetic'
                       and data['cookie'] == 'accessToken=ci-synthetic' and len(data['requestId']) == 32
                       for code, data in posts if code == 201), posts
            assert any(code == 201 for code, _ in posts)
            assert all(code in ([201,429] if args.enforce else [201]) for code, _ in posts)
            photos = [request('/api/trips/123/initial-attachments', b'photo') for _ in range(10)]
            assert all(code == 201 for code, _ in photos)
            reads = [request('/api/trips')[0] for _ in range(50)]
            assert 200 in reads and 429 in reads, reads
            # Logs are flushed per request; no live application data or credentials are present.
            logs = subprocess.check_output(['docker', 'logs', container], text=True, stderr=subprocess.DEVNULL)
            rows = [json.loads(line) for line in logs.splitlines() if line.startswith('{"method"')]
            creates = [x for x in rows if x['method'] == 'POST' and x['uri'] == '/api/trips']
            upload = [x for x in rows if x['uri'].endswith('/initial-attachments')]
            rejected = [x for x in creates if x['limit'] == ('REJECTED' if args.enforce else 'REJECTED_DRY_RUN')]
            assert len(creates) == 20 and rejected
            if args.enforce:
                assert all(x['status'] == 429 and x['upstream'] in ('', '-') and x['header_time'] in ('', '-') for x in rejected), rejected
            assert all(float(x['header_time']) >= 0 for x in creates if x['status'] == 201)
            assert len(upload) == 10 and all(x['limit'] == '' for x in upload)
            print(json.dumps({'nginx': '1.24.0-alpine', 'enforce': args.enforce, 'postStatus': dict(Counter(x[0] for x in posts)),
                'createVerdicts': dict(Counter(x['limit'] for x in creates)),
                'photoExcluded': len(upload), 'readStatus': dict(Counter(reads)),
                'methodBodyQueryCsrfCookieRequestIdPreserved': True}))
    finally:
        if container:
            subprocess.run(['docker', 'stop', '-t', '2', container], check=True, stdout=subprocess.DEVNULL)
        server.shutdown(); server.server_close()


if __name__ == '__main__':
    main()
