#!/usr/bin/env python3
"""One-shot loopback transfer for a session pool over two SSM port-forward sessions."""

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile


MAX_BYTES = 1024 * 1024


def validate_pool(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError("Pool exceeds 1 MiB")
    pool = json.loads(raw)
    if pool.get("version") != 1 or not isinstance(pool.get("sessions"), list) or not pool.get("runId"):
        raise ValueError("Invalid session pool")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("send", "receive"))
    parser.add_argument("--file", required=True)
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    path = Path(args.file)
    if args.mode == "send":
        if not path.is_file() or path.stat().st_mode & 0o077:
            raise SystemExit("Pool file must exist with mode 600")
        payload = path.read_bytes()
        validate_pool(payload)
    elif path.exists():
        raise SystemExit("Destination already exists")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Never log credentials or request metadata.

        def do_GET(self):
            if args.mode != "send" or self.path != "/pool":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            if args.mode != "receive" or self.path != "/pool":
                self.send_error(404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BYTES:
                    raise ValueError("Invalid length")
                data = self.rfile.read(size)
                validate_pool(data)
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if path.parent.stat().st_mode & 0o077:
                    raise ValueError("Destination directory must be private")
                descriptor, temporary = tempfile.mkstemp(prefix=".pool-", dir=path.parent)
                try:
                    os.fchmod(descriptor, 0o600)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(data)
                    os.link(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            except (ValueError, OSError, json.JSONDecodeError):
                self.send_error(400)
                return
            self.send_response(204)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    server.timeout = 120
    print(f"Listening on 127.0.0.1:{args.port} for one {args.mode} request (120s timeout)")
    server.handle_request()
    server.server_close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
