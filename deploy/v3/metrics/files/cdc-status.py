#!/usr/bin/env python3
"""Read only one Connect status endpoint. Never restart tasks or expose errors/config."""
import json
import os
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

CONNECTOR = 'yeodam-v3-outbox'
STATUS_URL = 'http://yeodam-v3-connect-connect-api.kafka.svc:8083/connectors/' + CONNECTOR + '/status'
STATES = ('RUNNING', 'FAILED', 'PAUSED', 'UNASSIGNED', 'RESTARTING', 'STOPPED')
MAX_BYTES = 65536

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def fetch_status():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(STATUS_URL, timeout=3) as response:
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Status too large')
    return json.loads(raw)

def metrics(fetch=fetch_status):
    started = time.monotonic()
    values = []
    try:
        status = fetch()
        if not isinstance(status, dict) or status.get('name') != CONNECTOR or not isinstance(status.get('tasks'), list):
            raise ValueError('Unexpected status contract')
        connector = status['connector']['state']
        if any(not isinstance(x, dict) for x in status['tasks']):
            raise ValueError('Invalid task contract')
        tasks = [x for x in status['tasks'] if type(x.get('id')) is int and x['id'] == 0]
        if connector not in STATES or len(tasks) > 1:
            raise ValueError('Unknown state or duplicate Task')
        if tasks and tasks[0].get('state') not in STATES:
            raise ValueError('Unknown Task state')
        values.append('yeodam_cdc_probe_success 1')
        values.append('yeodam_cdc_expected_task_present ' + str(int(len(tasks) == 1)))
        for state in STATES:
            values.append(f'yeodam_cdc_connector_state{{connector="{CONNECTOR}",state="{state}"}} {int(connector == state)}')
            values.append(f'yeodam_cdc_task_state{{connector="{CONNECTOR}",task="0",state="{state}"}} {int(bool(tasks) and tasks[0]["state"] == state)}')
    except (OSError, ValueError, KeyError, TypeError):
        # A failed observation is UNKNOWN, not a fabricated FAILED or RUNNING state.
        values = ['yeodam_cdc_probe_success 0']
    values.append(f'yeodam_cdc_probe_duration_seconds {time.monotonic() - started:.6f}')
    return '\n'.join(values) + '\n'

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/healthz':
            body, content_type = b'ok\n', 'text/plain'
        elif self.path == '/metrics':
            data = metrics()
            if os.environ.get('OUTBOX_ENABLED') == 'true':
                import importlib.util
                spec = importlib.util.spec_from_file_location('outbox_metrics', '/exporter/outbox-metrics.py')
                module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
                data += module.metrics()
            body, content_type = data.encode(), 'text/plain; version=0.0.4; charset=utf-8'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass

if __name__ == '__main__':
    HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
