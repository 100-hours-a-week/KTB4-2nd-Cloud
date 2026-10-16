"""Only the embedding model is replaced. Unexpected model calls fail closed."""
import base64
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from threading import Lock

from PIL import Image

COUNTS = Counter()
LOCK = Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Never log image payloads or URLs.

    def send_json(self, value, status=200):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/stats':
            with LOCK:
                self.send_json(dict(COUNTS))
        elif self.path == '/v1/models':
            self.send_json({'data': [{'id': 'ci-qwen'}]})
        else:
            self.send_json({'error': 'unexpected upstream request'}, 404)

    def do_POST(self):
        try:
            assert self.path == '/v1/embeddings'
            assert self.headers.get('Authorization') == 'Bearer ci-embedding-only'
            size = int(self.headers.get('Content-Length', '0'))
            assert 0 < size < 2_000_000
            data = json.loads(self.rfile.read(size))
            assert data['model'] == 'ci-qwen'
            item = data['messages'][1]['content'][0]
            assert item['type'] == 'image_url'
            raw = base64.b64decode(item['image_url']['url'].split(',', 1)[1], validate=True)
            with Image.open(io.BytesIO(raw)) as photo:
                photo.verify()
            with LOCK:
                COUNTS['image_embeddings'] += 1
            self.send_json({'data': [{'embedding': [1.0] + [0.0] * 2047}]})
        except (AssertionError, ValueError, KeyError, IndexError, OSError):
            with LOCK:
                COUNTS['unexpected_requests'] += 1
            self.send_json({'error': 'model contract rejected'}, 400)


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
