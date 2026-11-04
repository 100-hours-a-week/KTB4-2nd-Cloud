#!/usr/bin/env python3
"""Synthetic Docker-only WS startup check; no AWS, real user, cookie or Redis."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


class RejectAuthentication(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"message":"UNAUTHORIZED","data":null}')

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'ghcr.io/100-hours-a-week/yeodam-websocket@sha256:[0-9a-f]{64}', args.image):
        raise ValueError('Only immutable WebSocket images are accepted')
    prefix = 'ws-ci-' + uuid.uuid4().hex[:10]
    network, redis, websocket = prefix, prefix + '-redis', prefix + '-app'
    server = ThreadingHTTPServer(('0.0.0.0', 0), RejectAuthentication)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        docker('network', 'create', network)
        docker('run', '-d', '--name', redis, '--network', network, 'redis:7.4.2-alpine')
        docker('run', '-d', '--name', websocket, '--network', network,
               '--add-host', 'host.docker.internal:host-gateway', '--memory', '512m', '--cpus', '0.25',
               '-p', '127.0.0.1::8081', '-e', 'REDIS_HOST=' + redis, '-e', 'REDIS_SSL_ENABLED=false',
               '-e', 'REDIS_CONNECT_TIMEOUT=10s', '-e', 'REDIS_TIMEOUT=10s',
               '-e', 'WEBSOCKET_INTERNAL_SERVICE_TOKEN=ci-only-synthetic-token',
               '-e', 'WEBSOCKET_ALLOWED_ORIGINS=https://yeodam-2gether.com',
               '-e', f'BACKEND_INTERNAL_BASE_URL=http://host.docker.internal:{server.server_port}',
               '-e', 'JAVA_TOOL_OPTIONS=-XX:MaxRAMPercentage=50.0 -XX:InitialRAMPercentage=12.5', args.image)
        port = json.loads(docker('inspect', '--format', '{{json .NetworkSettings.Ports}}', websocket))['8081/tcp'][0]['HostPort']
        base = 'http://127.0.0.1:' + port
        ready = False
        for _ in range(90):
            state = json.loads(docker('inspect', '--format', '{{json .State}}', websocket))
            if not state['Running']:
                raise RuntimeError('WebSocket exited; OOMKilled=' + str(state.get('OOMKilled')))
            try:
                with urllib.request.urlopen(base + '/api/actuator/health/readiness', timeout=3) as response:
                    ready = response.status == 200
                if ready:
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                pass
            time.sleep(2)
        if not ready:
            raise RuntimeError('Redis subscription readiness did not become healthy')
        with urllib.request.urlopen(base + '/api/actuator/health/liveness', timeout=3) as response:
            assert response.status == 200
        try:
            urllib.request.urlopen(urllib.request.Request(base + '/api/ws', headers={'Origin': 'https://yeodam-2gether.com'}), timeout=5)
        except urllib.error.HTTPError as error:
            assert error.code == 401, 'Expected BE authentication rejection, got ' + str(error.code)
        else:
            raise RuntimeError('Unauthenticated WebSocket request was accepted')
        stats = json.loads(docker('stats', '--no-stream', '--format', '{{json .}}', websocket))
        print(json.dumps({'status': 'passed', 'cpu': '0.25', 'memoryLimit': '512MiB',
                          'memoryUsage': stats['MemUsage'], 'readiness': 200, 'liveness': 200,
                          'unauthenticatedHandshake': 401, 'authenticatedStomp': 'not-tested'}))
    except Exception:
        subprocess.run(['docker', 'logs', '--tail', '80', websocket], check=False)
        raise
    finally:
        for name in [websocket, redis]:
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['docker', 'network', 'rm', network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
