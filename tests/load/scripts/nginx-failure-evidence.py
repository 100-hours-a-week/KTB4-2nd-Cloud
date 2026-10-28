#!/usr/bin/env python3
"""Offline plan/readiness/correlation for existing collectors; never starts AWS or load."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
from uuid import UUID, uuid4

ORIGIN = 'https://v1-staging.yeodam-2gether.com'
ROLES = {'host': ('captureId', 5, 'sampleErrors'), 'ena': ('captureId', 5, 'errors'),
         'normal': ('normalId', 10, 'failureCount'), 'external': ('externalId', 10, 'errors'),
         'tcp': ('captureId', 5, 'parseErrors')}
MAX_BYTES = 50 * 1024**2


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('ISO timestamp required')
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('Timezone required')
    return d


def load(path, lines=False):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('Input exceeds 50 MiB')
    text = path.read_text()
    return [json.loads(x) for x in text.splitlines() if x] if lines else json.loads(text)


def manifest(root):
    c = load(root/'manifest.json')
    if c['origin'] != ORIGIN or not 10 <= c['seconds'] <= 1800:
        raise ValueError('Exact target and bounded duration required')
    for field in ['captureId', 'normalId', 'externalId']:
        UUID(c[field])
    if len({c[x] for x in ['captureId', 'normalId', 'externalId']}) != 3:
        raise ValueError('Independent collector IDs required')
    return c


def prepare(root, seconds):
    if not root.is_absolute() or not 10 <= seconds <= 1800:
        raise ValueError('New absolute directory and seconds 10..1800 required')
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    data = {'version': 1, 'origin': ORIGIN, 'seconds': seconds,
            'plannedAt': datetime.now(timezone.utc).isoformat(),
            **{k: str(uuid4()) for k in ['captureId', 'normalId', 'externalId']},
            'state': 'planned-only', 'loadStarted': False}
    for name in [*ROLES, 'client', 'app', 'private']:
        (root/name).mkdir(mode=0o700)
    write(root/'manifest.json', data)
    return {'plannedOnly': True, 'startedCollectors': 0, 'loadStarted': False}


def write(path, data):
    # Refuse symlinks/overwrite; diagnostics never print raw exception input.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(data, out, indent=2, ensure_ascii=False)
        out.write('\n')


def readiness(root, now):
    c = manifest(root)
    checks = {}
    for role, (id_field, max_age, error_field) in ROLES.items():
        issues = []
        try:
            x = load(root/role/'checkpoint.json')
            age = (now-timestamp(x['updatedAt'])).total_seconds()
            if x.get('runId') != c[id_field]: issues.append('run-id-mismatch')
            if not 0 <= age <= max_age: issues.append('stale-or-future')
            if x.get(error_field) != 0: issues.append('errors-or-missing-counter')
            if x.get('endedAt') or x.get('stopped') or x.get('reason'):
                issues.append('collector-ended-or-stopping')
            if role in ['ena', 'external'] and (x.get('failures') != 0 or x.get('guardTriggered') is not False):
                issues.append('probe-failed-or-guard-unknown')
            if role == 'ena' and (not isinstance(x.get('counterDelta'), dict) or
                                    not x['counterDelta'] or any(v != 0 for v in x['counterDelta'].values())):
                issues.append('ena-baseline-missing-or-exceeded')
            if role == 'tcp' and (x.get('discardedAfterBudget') != 0 or
                                  not isinstance(x.get('packets'), int) or x['packets'] < 1):
                issues.append('tcp-no-samples-or-budget-hit')
        except (OSError, ValueError, KeyError, TypeError):
            issues.append('missing-or-invalid-checkpoint')
        checks[role] = {'snapshotReady': not issues, 'issues': issues}
    binding_issues = []
    try:
        x = load(root/'source-binding.json')
        age = (now-timestamp(x['confirmedAt'])).total_seconds()
        if x.get('captureId') != c['captureId'] or not re.fullmatch('[a-f0-9]{32}', x.get('requestId', '')):
            binding_issues.append('invalid-source-binding')
        if x.get('networkChangedSinceConfirmation') is not False or not 0 <= age <= 300:
            binding_issues.append('source-may-have-changed')
        if x.get('serverPeerMatched') is not True or x.get('tcpFilterVerified') is not True:
            binding_issues.append('peer-or-filter-unverified')
        tcp = load(root/'tcp/checkpoint.json')
        if not re.fullmatch('[a-f0-9]{64}', str(x.get('filterSha256', ''))) or x.get('filterSha256') != tcp.get('filterSha256'):
            binding_issues.append('source-filter-plan-mismatch')
    except (OSError, ValueError, KeyError, TypeError):
        binding_issues.append('missing-source-binding')
    checks['source'] = {'snapshotReady': not binding_issues, 'issues': binding_issues}
    return {'checkedAt': now.isoformat(), 'checks': checks,
            'serverSnapshotReady': all(x['snapshotReady'] for x in checks.values()),
            'phoneNavigationCaptureVerified': False,
            'scope': 'Copied snapshots and operator source attestations only; not a load authorization or proof of live phone capture.'}


def tcp_patterns(packets):
    flows = defaultdict(list)
    for p in packets:
        if re.fullmatch('[a-f0-9]{20}', str(p.get('flowId', ''))):
            flows[p['flowId']].append(p)
    counts = Counter()
    for group in flows.values():
        syn = any(p.get('direction') == 'in' and p.get('flags') == 'S' for p in group)
        syn_ack = any(p.get('direction') == 'out' and p.get('flags') == 'S.' for p in group)
        # Just an observed flag pattern, not proof of an ordered TCP/TLS handshake.
        counts['syn-and-synack-observed' if syn and syn_ack else
               'syn-without-synack-in-window' if syn else 'no-initial-syn-in-window'] += 1
    return dict(counts)


def safe_route(value):
    # Client file must be the sanitizer output; only fixed route templates survive.
    allowed = {'/', '/login', '/trips', '/trips/create', '/mypage', '/auth/callback',
               '/api/actuator/health', '/api/users/me', '/api/trips', '/api/trips/map',
               '/api/auth/kakao/authorize', '/api/auth/kakao/callback', '/api/auth/csrf',
               '/api/auth/token/exchange', '/api/auth/token/refresh', '/api/auth/logout'}
    return value if value in allowed else '[other]'


def analyze(root):
    manifest(root)
    missing, hashes = [], {}

    def read(relative, default, lines=False):
        path = root/relative
        if not path.exists():
            missing.append(relative)
            return default
        result = load(path, lines)
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    mobile = read('client/har.json', {})
    if mobile and (mobile.get('version') != 2 or mobile.get('origin') != ORIGIN):
        raise ValueError('Version 2 redacted client metadata required')
    access = read('app/access.jsonl', [], True)
    packets = read('tcp/packets.jsonl', [], True)
    tcp_summary = read('tcp/summary.json', {})
    indexes = defaultdict(list)
    for x in access:
        if re.fullmatch('[a-f0-9]{32}', str(x.get('request_id', ''))):
            indexes[x['request_id']].append(x)
    requests = []
    for x in mobile.get('requests', []):
        rid = x.get('requestId')
        rows = indexes.get(rid, []) if isinstance(rid, str) else []
        statuses = [r['status'] for r in rows if isinstance(r.get('status'), int)]
        times = [r['request_time'] for r in rows if isinstance(r.get('request_time'), (int, float))
                 and math.isfinite(r['request_time'])]
        # Never match a request on timestamp/source alone or export untrusted raw fields.
        requests.append({'requestId': rid if isinstance(rid, str) and re.fullmatch('[a-f0-9]{32}', rid) else None,
                         'serverMatches': len(rows), 'serverStatuses': statuses,
                         'serverRequestSeconds': times})
    # Derive coverage from retained fixed URLs instead of trusting a manually edited coverage flag.
    urls = {x.get('url') for x in mobile.get('requests', []) if isinstance(x.get('url'), str)}
    coverage = {k: ORIGIN + path in urls for k, path in {
        'authorize': '/api/auth/kakao/authorize', 'callback': '/api/auth/kakao/callback',
        'exchange': '/api/auth/token/exchange', 'map': '/api/trips/map'}.items()}
    coverage['provider'] = any(x.get('role') == 'oauth-provider' and
                             x.get('url', '').startswith(('https://accounts.kakao.com/', 'https://kauth.kakao.com/'))
                             for x in mobile.get('requests', []))
    missing_phases = [k for k in ['authorize', 'provider', 'callback', 'exchange', 'map'] if coverage.get(k) is not True]
    context = manifest(root)
    expected = context['captureId']
    tcp_complete = (tcp_summary.get('runId') == expected and tcp_summary.get('complete') is True
                    and tcp_summary.get('kernelDropped') == 0 and tcp_summary.get('packets') == len(packets))
    summaries = {}
    for role, (id_field, _, error_field) in ROLES.items():
        data = tcp_summary if role == 'tcp' else read(role+'/summary.json', {})
        fields = ['sampleCount', error_field, 'failures', 'failureCount', 'sampleErrors', 'kernelDropped']
        summaries[role] = {'present': bool(data), 'runIdMatches': data.get('runId') == context[id_field],
                           'counts': {k: data[k] for k in fields if isinstance(data.get(k), int)},
                           'complete': data.get('complete') is True}
        if role == 'normal':
            summaries[role]['ownedCredentialsRevoked'] = data.get('credentialsRevoked') is True
    stages = Counter(x.get('failureStage') for x in mobile.get('requests', []) if x.get('failureStage') in
                     {'dns', 'tcp-connect', 'tls', 'browser-policy', 'client-canceled', 'transport-stage-unknown'})
    observation = read('client/observation.json', {})
    observed_at = observation.get('observedAt')
    try:
        observed_at = timestamp(observed_at).isoformat() if observed_at else None
    except (TypeError, ValueError, AttributeError):
        observed_at = None
    return {'rootCause': 'undetermined', 'missingFiles': missing, 'inputsSha256': hashes,
            'clientRequestCount': len(requests), 'requestIdCorrelation': requests,
            'collectorSummaries': summaries,
            'clientFailureStages': dict(stages),
            'observation': {'observedAt': observed_at, 'exactTimeKnown': observed_at is not None,
                            'route': safe_route(observation.get('route'))},
            'missingAuthPhases': missing_phases,
            'tcpComplete': tcp_complete, 'tcpPatterns': tcp_patterns(packets),
            'tcpNoPacketsMeaning': 'No server arrival inference without a valid source filter, matching window and complete capture.',
            'scope': 'An absent HTTP log, status 0 or SYN alone does not identify DNS/TCP/TLS root cause.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['prepare', 'check', 'analyze'])
    p.add_argument('--directory', type=Path, required=True)
    p.add_argument('--seconds', type=int, default=300)
    p.add_argument('--output', type=Path)
    a = p.parse_args()
    try:
        if not a.directory.is_absolute(): raise ValueError('Absolute directory required')
        if a.action == 'prepare': result = prepare(a.directory, a.seconds)
        elif a.action == 'check': result = readiness(a.directory, datetime.now(timezone.utc))
        else: result = analyze(a.directory)
        if a.output:
            if not a.output.is_absolute(): raise ValueError('Absolute output required')
            write(a.output, result)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if a.action == 'check' and not result['serverSnapshotReady'] else 0
    except (OSError, ValueError, TypeError, KeyError):
        p.exit(1, 'Evidence command refused: check schema, bounds and fresh output paths; no raw data printed.\n')


if __name__ == '__main__':
    raise SystemExit(main())
