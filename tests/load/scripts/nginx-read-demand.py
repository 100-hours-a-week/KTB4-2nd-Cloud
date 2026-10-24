#!/usr/bin/env python3
"""Capture completed V1 staging read requests and model rate/burst offline.

No HTTP requests, token access, load generation or Nginx limit changes.
Arrival is estimated as completion msec minus request_time, not limiter-phase time.
"""
import argparse
import bisect
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import time
import uuid

BASE = Path('/var/lib/yeodam/nginx-read-demand')
LOG = Path('/var/log/nginx/yeodam-access.json')
EXCLUDED = {'/api/auth/csrf', '/api/auth/kakao/authorize',
            '/api/auth/kakao/callback', '/api/actuator/health'}
PHASES = ['single-1', 'single-2', 'single-3', 'multi-tab']
CAPTURE_PHASES = PHASES + ['normal-before', 'enforced-load', 'normal-after', 'upload-10', 'upload-150']


def utc(ms):
    return datetime.fromtimestamp(ms/1000, timezone.utc).isoformat(timespec='milliseconds')


def now_ms():
    return time.time_ns()//1_000_000


def millis(value):
    try:
        d = Decimal(str(value))
        if not d.is_finite() or d < 0:
            raise ValueError('Invalid nonnegative time')
        return int(d*1000)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('Invalid numeric time; row contents withheld') from None


def read_api(row):
    route = row.get('normalized_route', '')
    return (row.get('method') in ('GET', 'HEAD') and
            route.startswith('/api/') and route not in EXCLUDED)


def peak_count(times, window_ms):
    """Maximum count in a sliding (t-window, t] interval."""
    times = sorted(times)
    left = best = 0
    for right, end in enumerate(times):
        while times[left] <= end-window_ms:
            left += 1
        best = max(best, right-left+1)
    return best


def required_burst(times, rate):
    """Minimum integer burst for a no-rejection continuous leaky-bucket model."""
    times = sorted(times)
    excess = high = Decimal(0)
    rate = Decimal(str(rate))
    if not rate.is_finite() or rate <= 0:
        raise ValueError('Rate must be finite and positive')
    for before, after in zip(times, times[1:]):
        excess = max(Decimal(0), excess-rate*Decimal(after-before)/1000+1)
        high = max(high, excess)
    return math.ceil(high)


def replay_rejections(times, rate, burst):
    """Rejected arrivals do not consume accepted-request bucket state."""
    times = sorted(times)
    if not times:
        return 0
    rate = Decimal(str(rate))
    if not rate.is_finite() or rate <= 0 or burst < 0:
        raise ValueError('Invalid rate or burst')
    excess, last, rejected = Decimal(0), times[0], 0
    for t in times[1:]:
        candidate = max(Decimal(0), excess-rate*Decimal(t-last)/1000+1)
        if candidate > burst:
            rejected += 1
        else:
            excess, last = candidate, t
    return rejected


def statistics(events, rates):
    by_source = defaultdict(list)
    for row in events:
        by_source[row['source_id']].append(row['start_ms'])
    result = []
    for source, times in sorted(by_source.items()):
        selected = [x for x in events if x['source_id'] == source]
        peaks = {str(w): peak_count(times, w*1000) for w in (1, 10, 30)}
        # A tradeoff frontier from observations, not an automatically selected policy.
        candidates = rates or sorted({max(1, math.ceil(peaks[str(w)]/w)) for w in (1, 10, 30)})
        frontier = []
        for rate in candidates:
            burst = required_burst(times, rate)
            frontier.append({'rate_rps': rate, 'required_burst': burst,
                             'modeled_rejections': replay_rejections(times, rate, burst)})
        result.append({'source_id': source, 'requests': len(times),
                       'peer_scope': selected[0].get('peer_scope', 'not_recorded'),
                       'first_start_utc': utc(min(times)), 'last_start_utc': utc(max(times)),
                       'max_sliding_counts': peaks,
                       'max_10s_rps': peaks['10']/10, 'max_30s_rps': peaks['30']/30,
                       'routes': dict(Counter(x['route'] for x in selected)),
                       'statuses': dict(Counter(str(x['status']) for x in selected)),
                       'upstream_statuses': dict(Counter(x.get('upstream_status', 'not_logged') for x in selected)),
                       'limit_req_statuses': dict(Counter(x.get('limit_req_status', 'not_logged') for x in selected)),
                       'rate_burst_tradeoffs': frontier})
    return result


def report(events, phases, rates):
    groups = {'all': events}
    groups.update({p: [x for x in events if x['phase'] == p] for p in phases})
    counts = dict(Counter(x['phase'] for x in events))
    return {'schema': 1, 'generated_at_utc': utc(now_ms()),
            'measurement': 'completed matched read API requests only',
            'timing': 'estimated request start = completion msec - request_time; millisecond precision',
            'model': 'continuous leaky bucket; not exact Nginx phase timing or capacity proof',
            'policy_selected': False, 'request_count': len(events),
            'phase_counts': counts, 'all_four_phases_have_requests': all(counts.get(x, 0) for x in PHASES),
            'groups': {name: statistics(rows, rates) for name, rows in groups.items()}}


def sanitize(rows, meta, end, key):
    events, skipped, seen = [], Counter(), set()
    phases = meta['phases']
    starts = [p['start_ms'] for p in phases]
    for row in rows:
        if 'msec' not in row:
            # Old rotated records are normally outside this capture. Refuse a
            # mixed-format record inside it instead of silently losing requests.
            ts = row.get('timestamp')
            if ts:
                try:
                    old_ms = int(datetime.fromisoformat(ts).timestamp()*1000)
                except ValueError:
                    raise ValueError('Invalid log timestamp; row contents withheld') from None
                if meta['start_ms'] <= old_ms <= end:
                    raise ValueError('Missing msec within capture window')
            continue
        done = millis(row['msec'])
        if done < meta['start_ms'] or done > end:
            continue
        if not read_api(row):
            skipped['non_selected_requests'] += 1
            if int(row.get('status', 0)) >= 400:
                skipped['non_selected_status_'+str(row['status'])] += 1
            continue
        start = done-millis(row.get('request_time'))
        if start < meta['start_ms']:
            skipped['started_before_capture'] += 1
            continue
        if row.get('limit_req_status', 'not_logged') not in ('not_logged', '', '-', 'PASSED', 'DELAYED', 'REJECTED', 'DELAYED_DRY_RUN', 'REJECTED_DRY_RUN'):
            raise ValueError('Unknown limit decision; row contents withheld')
        upstream = str(row.get('upstream_status', 'not_logged'))
        if upstream not in ('not_logged', '') and not re.fullmatch(r'(?:-|[1-5][0-9]{2})(?:[, :]+(?:-|[1-5][0-9]{2}))*', upstream):
            raise ValueError('Invalid upstream status; row contents withheld')
        address = ipaddress.ip_address(row.get('peer_addr', ''))
        peer = str(address)
        scope = 'loopback' if address.is_loopback else ('private' if address.is_private else 'public')
        request_id = row.get('request_id', '')
        if not re.fullmatch(r'[a-zA-Z0-9-]{16,64}', request_id):
            raise ValueError('Missing or invalid request_id')
        if request_id in seen:
            raise ValueError('Duplicate request_id; inspect log rotation/export')
        seen.add(request_id)
        phase = phases[bisect.bisect_right(starts, start)-1]['name']
        source = hmac.new(key, peer.encode(), hashlib.sha256).hexdigest()[:16]
        events.append({'start_ms': start, 'done_ms': done, 'phase': phase,
                       'source_id': source, 'request_id': request_id,
                       'peer_scope': scope,
                       'route': row['normalized_route'], 'method': row['method'],
                       'status': int(row['status']),
                       'limit_req_status': row.get('limit_req_status', 'not_logged'),
                       'upstream_status': upstream,
                       'request_time_ms': done-start})
    return sorted(events, key=lambda x: (x['start_ms'], x['request_id'])), dict(skipped)


def log_rows(start):
    for path in sorted(LOG.parent.glob(LOG.name+'*')):
        if not re.fullmatch(re.escape(LOG.name)+r'(?:\.\d+(?:\.gz)?)?', path.name):
            continue
        if path.stat().st_mtime*1000 < start-2000:
            continue
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rt', encoding='utf-8') as f:
            for line in f:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    raise ValueError('Malformed access log; export is incomplete') from None


def require_app():
    if os.geteuid() != 0 or Path('/var/lib/cloud/data/instance-id').read_text().strip() != 'i-05d320e3a87200d5a':
        raise ValueError('Capture commands require root on the V1 staging App')


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
    path.chmod(0o600)


def current():
    run_id = (BASE/'current').read_text().strip()
    uuid.UUID(run_id)
    directory = BASE/run_id
    meta = json.loads((directory/'capture.json').read_text())
    if meta.get('end_ms'):
        raise ValueError('Capture already finished; begin a new capture')
    if now_ms()-meta['start_ms'] > 30*60*1000:
        raise ValueError('Capture exceeded 30 minutes; abandon and begin a new one')
    return directory, meta


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='mode', required=True)
    begin = sub.add_parser('begin')
    begin.add_argument('--abandon-current', action='store_true')
    begin.add_argument('--phase', choices=CAPTURE_PHASES, default='single-1')
    mark = sub.add_parser('mark')
    mark.add_argument('--phase', choices=CAPTURE_PHASES, required=True)
    sub.add_parser('finish')
    offline = sub.add_parser('analyze')
    offline.add_argument('--input', type=Path, required=True)
    offline.add_argument('--rates', type=int, nargs='+')
    a = p.parse_args()
    if a.mode == 'analyze':
        events = [json.loads(x) for x in a.input.read_text().splitlines() if x.strip()]
        if not events:
            raise ValueError('No captured read requests')
        print(json.dumps(report(events, sorted({x['phase'] for x in events}), a.rates), ensure_ascii=False, indent=2))
        return
    require_app()
    if a.mode == 'begin':
        if (BASE/'current').exists() and not a.abandon_current:
            try:
                current()
            except ValueError:
                pass
            else:
                raise ValueError('Capture active; finish or explicitly abandon it')
        site = Path('/etc/nginx/sites-enabled/yeodam-v1-staging').read_text()
        if '$msec' not in site or '$remote_addr' not in site or '"peer_addr"' not in site.replace('\\"', '"'):
            raise ValueError('Measurement fields not installed')
        start = now_ms()
        meta = {'run_id': str(uuid.uuid4()), 'start_ms': start, 'start_utc': utc(start),
                'site_sha256': hashlib.sha256(site.encode()).hexdigest(),
                'phases': [{'name': a.phase, 'start_ms': start}]}
        BASE.mkdir(parents=True, exist_ok=True, mode=0o700)
        BASE.chmod(0o700)
        directory = BASE/meta['run_id']
        directory.mkdir(parents=True, mode=0o700)
        save(directory/'capture.json', meta)
        (directory/'peer.key').write_bytes(secrets.token_bytes(32))
        (directory/'peer.key').chmod(0o600)
        (BASE/'current').write_text(meta['run_id'])
        (BASE/'current').chmod(0o600)
        print(json.dumps({'run_id': meta['run_id'], 'phase': a.phase, 'start_utc': utc(start)}))
        return
    directory, meta = current()
    if a.mode == 'mark':
        if a.phase in [x['name'] for x in meta['phases']]:
            raise ValueError('Phase already recorded')
        meta['phases'].append({'name': a.phase, 'start_ms': now_ms()})
        save(directory/'capture.json', meta)
        print(json.dumps({'phase': a.phase, 'utc': utc(meta['phases'][-1]['start_ms'])}))
        return
    # Stop UI activity before finish. This quiet tail allows ordinary short reads
    # to complete, but does not prove that every in-flight request has completed.
    time.sleep(5)
    end = now_ms()
    if end-meta['start_ms'] > 30*60*1000:
        raise ValueError('Capture exceeded 30 minutes')
    events, skipped = sanitize(log_rows(meta['start_ms']), meta, end, (directory/'peer.key').read_bytes())
    if not events:
        raise ValueError('No matched read requests; keep capture and inspect origin/logs')
    data = ''.join(json.dumps(x, ensure_ascii=False)+'\n' for x in events)
    (directory/'events.jsonl').write_text(data)
    (directory/'events.jsonl').chmod(0o600)
    summary = report(events, [x['name'] for x in meta['phases']], None)
    site = Path('/etc/nginx/sites-enabled/yeodam-v1-staging').read_bytes()
    summary.update({'capture_start_utc': meta['start_utc'], 'capture_end_utc': utc(end),
                    'site_unchanged': hashlib.sha256(site).hexdigest() == meta['site_sha256'],
                    'capture_seconds': (end-meta['start_ms'])/1000,
                    'quiet_tail_seconds': (end-max(x['done_ms'] for x in events))/1000,
                    'skipped': skipped,
                    'events_sha256': hashlib.sha256(data.encode()).hexdigest()})
    save(directory/'summary.json', summary)
    meta.update({'end_ms': end, 'end_utc': utc(end)})
    save(directory/'capture.json', meta)
    print(json.dumps({'directory': str(directory), 'summary': summary}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError):
        # Do not echo bad input, peer addresses, file content or request headers.
        raise SystemExit('Read-demand capture failed; check fields, active capture and input schema using the runbook.') from None
