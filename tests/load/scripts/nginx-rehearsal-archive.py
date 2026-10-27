#!/usr/bin/env python3
"""Repeatable, private App/PLG archives. Never export original IPs, cookies or raw log files."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tarfile
import urllib.parse
import urllib.request
from uuid import UUID, uuid4

APP = 'i-05d320e3a87200d5a'
PLG = 'i-05c76ccb2b53f9279'
GENERATOR = 'i-0c53b65eb35dd2b42'
ROOT = Path('/var/lib/yeodam/nginx-rehearsal')
FIELDS = ['timestamp', 'msec', 'request_id', 'method', 'normalized_route', 'status', 'upstream_status',
          'request_time', 'upstream_connect_time', 'upstream_header_time', 'upstream_response_time', 'request_length', 'body_bytes_sent',
          'connection', 'connection_requests', 'worker_pid', 'http_protocol', 'limit_req_status']
ERRORS = ['worker_connections are not enough', 'too many open files', 'upstream timed out',
          'connect() failed', 'upstream prematurely closed connection', 'limiting requests', 'limiting connections']


def utc():
    return datetime.now(timezone.utc).isoformat()


def ns(value):
    return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()*1e9)


def dump(path, value):
    path.write_text(json.dumps(value, indent=2))


def scrub(value, key):
    if isinstance(value, dict):
        return {k: scrub(v, key) for k, v in value.items()
                if not re.search(r'token|cookie|authorization|password|secret|credential|signature|api_key|access_key|object_key|url', k, re.I)}
    if isinstance(value, list):
        return [scrub(v, key) for v in value]
    if isinstance(value, str):
        try:
            ipaddress.ip_address(value)
            return 'peer-'+hmac.new(key, value.encode(), hashlib.sha256).hexdigest()[:16]
        except ValueError:
            pass
        value = re.sub(r'(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![\w.])', lambda m: 'peer-'+hmac.new(key, m[0].encode(), hashlib.sha256).hexdigest()[:16], value)
        value = re.sub(r'(?i)(authorization|set-cookie|cookie)\s*:\s*[^\r\n]+', r'\1: [REDACTED]', value)
        value = re.sub(r'(https?://[^\s\"<>?]+)\?[^\s\"<>]+', r'\1?[REDACTED]', value)
        value = re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[JWT]', value)
        value = re.sub(r'(?i)(bearer|cookie|authorization|password|token)\s*[:= ]\s*[^\s,;]+', r'\1=[REDACTED]', value)
    return value


def access_row(data, key):
    row = {k: data[k] for k in FIELDS if k in data}
    peer = data.get('peer_addr', '')
    row['sourceId'] = hmac.new(key, peer.encode(), hashlib.sha256).hexdigest()[:16]
    return scrub(row, key)


def get(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        data = json.load(response)
    if data.get('status') != 'success':
        raise ValueError('Query did not succeed')
    return data


def loki_pages(fetch, query, start, end, emit, limit=1000, max_queries=2000):
    """Bisect saturated [start,end) windows. Fail closed for a saturated single timestamp."""
    calls = 0
    def visit(left, right):
        nonlocal calls
        calls += 1
        if calls > max_queries:
            raise ValueError('Loki query budget exceeded; archive incomplete')
        data = fetch(query, left, right, limit)
        rows = [(int(at), line) for stream in data['data']['result'] for at, line in stream['values']]
        if len(rows) >= limit:
            if right-left <= 1:
                raise ValueError('Single timestamp exceeds limit; do not skip tied entries')
            middle = (left+right)//2
            visit(left, middle)
            visit(middle, right)
        else:
            for at, line in rows:
                if left <= at < right:
                    emit(at, line)
    # Small outer windows bound expensive individual queries; full pages still subdivide.
    for left in range(start, end, 30_000_000_000):
        visit(left, min(end, left+30_000_000_000))
    return calls


def file_hash(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024*1024):
            result.update(chunk)
    return result.hexdigest()


def seal(folder):
    files = {p.name: file_hash(p) for p in folder.iterdir() if p.is_file()}
    dump(folder/'checksums.json', files)
    archive = folder.with_suffix('.tar.gz')
    with tarfile.open(archive, 'w:gz') as tar:
        for path in sorted(folder.iterdir()):
            tar.add(path, arcname=path.name, recursive=False)
    print(json.dumps({'archive': str(archive), 'size': archive.stat().st_size,
                      'sha256': file_hash(archive)}))


def app_archive(run):
    source = ROOT/run
    plan = json.loads((source/'plan.json').read_text())
    final = (source/'result.json').exists()
    if not final and subprocess.run(['systemctl','is-active','--quiet',plan['unit']]).returncode == 0:
        raise ValueError('Stop the active capture before a consistent export')
    state = json.loads((source/('result.json' if final else 'checkpoint.json')).read_text())
    destination = source/'exports'/str(uuid4())
    destination.mkdir(parents=True)
    key_file = source/'peer.private.key'
    try:
        with key_file.open('xb') as stream:
            stream.write(secrets.token_bytes(32))
    except FileExistsError:
        pass
    key = key_file.read_bytes()
    for name in ['plan.json', 'result.json', 'checkpoint.json', 'marks.jsonl', 'samples.jsonl', 'journey-audit.json']:
        if (source/name).exists():
            shutil.copyfile(source/name, destination/name)
    # #438 opt-in ENA observer; absence is not interpreted as zero allowance drops.
    for name in ['samples.jsonl', 'summary.json']:
        if (source/'ena'/name).is_file():
            shutil.copyfile(source/'ena'/name, destination/('ena-'+name))
    normal_runs = list(dict.fromkeys([run] + plan.get('normalRunIds', [])))
    if len(normal_runs) > 16:
        raise ValueError('Normal capture reference budget exceeded')
    for normal_run in normal_runs:
        if str(UUID(normal_run)) != normal_run:
            raise ValueError('Only canonical normal capture UUIDs are allowed')
        normal = Path('/var/lib/yeodam/nginx-connections-426')/normal_run
        prefix = 'normal-' if normal_run == run else 'normal-'+normal_run+'-'
        for name in ['samples.jsonl', 'summary.json', 'checkpoint.json']:
            if (normal/name).exists():
                shutil.copyfile(normal/name, destination/(prefix+name))
    start, end = ns(state['startedAt']), ns(state.get('endedAt', state['updatedAt']))
    statuses, error_counts = Counter(), Counter()
    parsed = 0
    malformed = 0
    with (destination/'access.jsonl').open('w') as out:
        with (source/'access.private.raw').open() as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                    at = int(float(value['msec'])*1e9)
                    if not start <= at <= end:
                        continue
                    row = access_row(value, key)
                    statuses[str(row['status'])] += 1
                    parsed += 1
                    out.write(json.dumps(row)+'\n')
                except (ValueError, KeyError):
                    malformed += 1
    # Keep original errors private on App; exported unknown errors get a fingerprint, not raw text.
    with (destination/'errors.jsonl').open('w') as out:
        for line in (source/'error.private.raw').open():
            category = next((x for x in ERRORS if x in line.lower()), 'other')
            error_counts[category] += 1
            out.write(json.dumps({'timestamp': line[:19], 'category': category,
                                 'sha256': hashlib.sha256(line.encode()).hexdigest()})+'\n')
    dump(destination/'index.json', {'runId': run, 'from': state['startedAt'],
         'to': state.get('endedAt', state['updatedAt']), 'exportedAt': utc(), 'finalResultAvailable': final,
         'collectionComplete': final and state['collectionComplete'] and not malformed,
         'siteUnchanged': state.get('siteUnchanged'), 'accessEvents': parsed, 'statuses': statuses,
         'errorCounts': error_counts, 'malformedAccessLines': malformed,
         'privateOriginalsExported': False, 'normalUserSuccessVerified': False})
    seal(destination)


def generator_archive(run, tls=False):
    source = Path('/var/lib/yeodam/nginx-tls440' if tls else '/var/lib/yeodam/nginx-edge438')/run
    destination = ROOT/run/'exports'/str(uuid4())
    destination.mkdir(parents=True)
    # Only #438's credential-free scenarios, never generic k6 output or session pools.
    allowed = {'http100', 'http100-csrf-stop', 'http450', 'http450-startup-drop',
               'hold192', 'churn120', 'mixed450'}
    if tls:
        allowed = {'reuse10a', 'fresh10a', 'fresh10b', 'reuse10b',
                   'reuse100', 'reuse450', 'tls10', 'fresh20'}
    for stage in source.iterdir():
        if stage.name not in allowed or not stage.is_dir():
            raise ValueError('Unexpected generator stage')
        for name in (['result.json', 'load.log'] if tls else ['http-summary.json', 'tcp.json', 'http.log', 'tcp.log']):
            if (stage/name).is_file():
                shutil.copyfile(stage/name, destination/(stage.name+'-'+name))
    replay = Path('/opt/yeodam/ops/nginx-tls440/nginx-http-tls-load.py' if tls else
                  '/opt/yeodam/ops/nginx-edge438/nginx-edge-replay.js')
    shutil.copyfile(replay, destination/('load-source.py' if tls else 'replay-source.js'))
    if tls:
        shutil.copyfile(replay.with_name('nginx-tcp-capacity.py'), destination/'protocol-source.py')
    dump(destination/'index.json', {'runId':run, 'scope':'Credential-free #440 generator results only' if tls
                                  else 'Credential-free #438 generator results only'})
    seal(destination)


def plg_archive(run, start_text, end_text, lookahead=60):
    start, end = ns(start_text), ns(end_text)
    if not 0 < end-start <= 7200*1e9 or end > int(datetime.now(timezone.utc).timestamp()*1e9)+1e9:
        raise ValueError('Use a completed, bounded window of at most2 hours')
    folder = Path('/var/lib/yeodam/nginx-rehearsal-plg')/run/str(uuid4())
    folder.mkdir(parents=True)
    key = secrets.token_bytes(32)
    dashboard = json.loads(Path('/opt/yeodam/plg/grafana/dashboards/v1-source.json').read_text())
    dump(folder/'dashboard.json', dashboard)
    panels = list(dashboard['panels'])
    tcp_path = Path('/opt/yeodam/plg/grafana/dashboards/v1-tcp-defense.json')
    if tcp_path.is_file():
        tcp_dashboard = json.loads(tcp_path.read_text())
        dump(folder/'tcp-dashboard.json', tcp_dashboard)
        panels += [dict(panel, archivePrefix='tcp-') for panel in tcp_dashboard['panels']]
    report = {'runId': run, 'from': start_text, 'to': end_text, 'exportedAt': utc(), 'queries': [], 'errors': []}
    for panel in panels:
        if panel.get('type') == 'logs':
            continue
        for index, target in enumerate(panel.get('targets', [])):
            expr = target.get('expr')
            if not expr:
                continue
            engine = 'loki' if 'service_name=' in expr else 'prometheus'
            expr = expr.replace('{service_name="v1-nginx"', '{environment="staging",service_name="v1-nginx"')
            params = {'query': expr, 'start': str(start) if engine == 'loki' else str(start/1e9),
                      'end': str(end) if engine == 'loki' else str(end/1e9), 'step': '15'}
            prefix = 'http://127.0.0.1:3100/loki/api/v1/query_range' if engine == 'loki' else 'http://127.0.0.1:9090/api/v1/query_range'
            filename = panel.get('archivePrefix','')+'panel-'+str(panel['id'])+'-'+str(index)+'.json'
            entry = {'panelId': panel['id'], 'title': panel['title'], 'expression': expr, 'engine': engine, 'file': filename}
            try:
                data = get(prefix+'?'+urllib.parse.urlencode(params))
                dump(folder/filename, scrub(data, key))
                entry['series'] = len(data['data']['result'])
            except Exception as error:
                entry['errorType'] = type(error).__name__
                report['errors'].append(filename)
            report['queries'].append(entry)
    for stream_name in ['access', 'error']:
        def fetch(query, left, right, limit):
            params = {'query': query, 'start': left, 'end': right, 'direction': 'forward', 'limit': limit}
            return get('http://127.0.0.1:3100/loki/api/v1/query_range?'+urllib.parse.urlencode(params))
        count = 0
        with (folder/('loki-'+stream_name+'.jsonl')).open('w') as output:
            def emit(at, line):
                nonlocal count
                try:
                    if stream_name == 'access':
                        original = json.loads(line)
                        if not start <= int(float(original['msec'])*1e9) <= end:
                            return
                    value = access_row(original, key) if stream_name == 'access' else {
                        'category': next((x for x in ERRORS if x in line.lower()), 'other'),
                        'sha256': hashlib.sha256(line.encode()).hexdigest()}
                except ValueError:
                    value = {'malformed': True, 'sha256': hashlib.sha256(line.encode()).hexdigest()}
                output.write(json.dumps({'atNs': at, 'data': value})+'\n')
                count += 1
            try:
                calls = loki_pages(fetch, '{environment="staging",service_name="v1-nginx",stream="'+stream_name+'"}', start-2_000_000_000, end+lookahead*1_000_000_000, emit)
                report[stream_name] = {'entries': count, 'queries': calls, 'complete': True}
            except Exception as error:
                report[stream_name] = {'entries': count, 'complete': False, 'errorType': type(error).__name__}
                report['errors'].append('loki-'+stream_name)
    report['archiveComplete'] = not report['errors']
    report['lokiIngestionPaddingSeconds'] = {'before':2,'after':lookahead}
    report['stepSeconds'] = 15
    report['grafanaUrl'] = ('https://grafana-staging.yeodam-2gether.com/d/yeodam-v1-source?from='
                            +str(start//1_000_000)+'&to='+str(end//1_000_000)+'&timezone=utc')
    dump(folder/'index.json', report)
    seal(folder)


def serve(path):
    path = path.resolve()
    if (not any(path.is_relative_to(root) for root in [ROOT, Path('/var/lib/yeodam/nginx-rehearsal-plg')])
            or path.suffixes[-2:] != ['.tar', '.gz'] or path.stat().st_size > 512*1024*1024):
        raise ValueError('Only a bounded task archive may be served')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            if self.path != '/archive':
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Length', str(path.stat().st_size))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            with path.open('rb') as stream:
                shutil.copyfileobj(stream, self.wfile)
    server = HTTPServer(('127.0.0.1', 19426), Handler)
    server.timeout = 120
    server.handle_request()
    server.server_close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['app', 'plg', 'generator', 'tls-generator', 'serve'])
    p.add_argument('--run-id')
    p.add_argument('--from', dest='start')
    p.add_argument('--to', dest='end')
    p.add_argument('--archive', type=Path)
    p.add_argument('--loki-lookahead-seconds', type=int, default=60)
    a = p.parse_args()
    os.umask(0o077)
    if not 0 <= a.loki_lookahead_seconds <= 3600:
        p.error('Loki ingestion padding must be0..3600s')
    if a.mode != 'serve' and shutil.disk_usage('/').used/shutil.disk_usage('/').total >= .8:
        p.error('Disk exceeds80%; preserve source and export after review')
    instance = Path('/var/lib/cloud/data/instance-id').read_text().strip()
    if os.geteuid() != 0 or instance not in [APP, PLG, GENERATOR]:
        p.error('Only the exact staging App, PLG or generator root is allowed')
    if a.mode != 'serve':
        UUID(a.run_id)
    if a.mode == 'app' and instance == APP:
        app_archive(a.run_id)
    elif a.mode == 'plg' and instance == PLG:
        plg_archive(a.run_id, a.start, a.end, a.loki_lookahead_seconds)
    elif a.mode == 'generator' and instance == GENERATOR:
        generator_archive(a.run_id)
    elif a.mode == 'tls-generator' and instance == GENERATOR:
        generator_archive(a.run_id, tls=True)
    elif a.mode == 'serve' and a.archive:
        serve(a.archive)
    else:
        p.error('Mode differs from this host')


if __name__ == '__main__':
    main()
