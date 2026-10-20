#!/usr/bin/env python3
"""One-shot loopback-only SSM tunnel bridge for an AES key; never print the key."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import re


def checked(path):
    data = Path(path).read_bytes().strip()
    if not re.fullmatch(b'[0-9a-fA-F]{64}', data):
        raise ValueError('invalid 32-byte AES key file')
    return data


def run(mode, path, port):
    if mode == 'serve':
        data = checked(path)
    else:
        data = None
        if Path(path).exists():
            raise FileExistsError('refuse to overwrite an existing auth key')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if mode != 'serve' or self.path != '/auth368':
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_PUT(self):
            if mode != 'receive' or self.path != '/auth368' or self.headers.get('Content-Length') != '64':
                self.send_error(400)
                return
            value = self.rfile.read(64)
            if not re.fullmatch(b'[0-9a-fA-F]{64}', value):
                self.send_error(400)
                return
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(value)
            self.send_response(204)
            self.end_headers()

    server = HTTPServer(('127.0.0.1', port), Handler)
    server.timeout = 120
    try:
        server.handle_request()
    finally:
        server.server_close()
    if mode == 'serve':
        print('served one key; sha256-prefix=' + hashlib.sha256(data).hexdigest()[:16])
    elif Path(path).exists():
        print('received one key; sha256-prefix=' + hashlib.sha256(checked(path)).hexdigest()[:16])


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('mode', choices=('serve', 'receive'))
    p.add_argument('path')
    p.add_argument('--port', type=int, default=19080)
    a = p.parse_args()
    if not 1024 < a.port < 65536:
        raise ValueError('invalid loopback port')
    run(a.mode, a.path, a.port)
