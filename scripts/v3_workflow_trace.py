"""Read-only, bounded evidence for an explicitly sampled experiment request.

No business completeness/exactly-once claim: compare database/Outbox evidence separately.
"""
import base64
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request

BOUNDARIES = ('photo.convert', 'trip.process.dispatch', 'ai.receive', 'ai.execute',
              'ai.publish', 'trip.result.apply')
MAX_BYTES = 8 * 1024 * 1024


def trial():
    return {'schemaVersion': 1, 'traceparent': '00-' + secrets.token_hex(16) + '-'
            + secrets.token_hex(8) + '-01'}


def identifier(value, length):
    if not isinstance(value, str):
        raise ValueError('Invalid trace identifier')
    if re.fullmatch('[0-9a-fA-F]{%d}' % length, value):
        result = value.lower()
    else:
        try:
            result = base64.b64decode(value, validate=True).hex()
        except (ValueError, TypeError):
            raise ValueError('Invalid trace identifier') from None
    if len(result) != length or not int(result, 16):
        raise ValueError('Invalid trace identifier')
    return result


def trace_id(manifest):
    parent = manifest.get('traceparent', '')
    if manifest.get('schemaVersion') != 1 or not re.fullmatch(
            '00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}', parent):
        raise ValueError('Invalid trial manifest')
    identifier(parent[3:35], 32)
    identifier(parent[36:52], 16)
    if not int(parent[-2:], 16) & 1:
        raise ValueError('Trial parent is not sampled; never rewrite existing task context')
    return parent[3:35]


def read_tempo(base, tid):
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1') \
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('Use a local Tempo port-forward URL only')
    tid = identifier(tid, 32)
    # Do not follow redirects from the local endpoint or use environment HTTP proxies.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    req = urllib.request.Request(base.rstrip('/') + '/api/traces/' + tid,
                                 headers={'Accept': 'application/json'})
    try:
        with opener.open(req, timeout=10) as response:
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as failure:
        if failure.code == 404:
            return None
        raise ValueError('Tempo HTTP failure: %d' % failure.code) from None
    if len(raw) > MAX_BYTES:
        raise ValueError('Trace exceeds evidence read limit')
    return json.loads(raw)


def inspect_trace(body, tid):
    if body is None:
        return {'status': 'missing_trace', 'traceId': tid, 'tracePathComplete': False}
    # Tempo JSON uses batches; OTLP fixtures use resourceSpans.
    batches = body.get('batches', body.get('resourceSpans', []))
    spans = {}
    for batch in batches:
        attrs = batch.get('resource', {}).get('attributes', [])
        service = next((a.get('value', {}).get('stringValue') for a in attrs
                        if a.get('key') == 'service.name'), None)
        for scope in batch.get('scopeSpans', batch.get('instrumentationLibrarySpans', [])):
            for span in scope.get('spans', []):
                if identifier(span.get('traceId'), 32) != tid:
                    raise ValueError('Mixed trace response')
                sid = identifier(span.get('spanId'), 16)
                parent = span.get('parentSpanId')
                parent = identifier(parent, 16) if parent else None
                if sid in spans or parent == sid:
                    raise ValueError('Duplicate span or self parent')
                # Export only fixed service/name and topology; no arbitrary attributes/events.
                spans[sid] = {'id': sid, 'parent': parent,
                              'service': service if service in ('photo-converter', 'trip-result-worker',
                                          'ai-api', 'ai-runner', 'trip-api', 'photo-api') else 'other',
                              'name': span.get('name') if span.get('name') in BOUNDARIES else 'other'}
    for sid in spans:
        seen = set()
        current = sid
        while current in spans:
            if current in seen:
                raise ValueError('Span parent cycle')
            seen.add(current)
            current = spans[current]['parent']
    required = (('photo-converter', 'photo.convert'), ('trip-result-worker', 'trip.process.dispatch'),
                ('ai-api', 'ai.receive'), ('ai-runner', 'ai.execute'),
                ('ai-api', 'ai.publish'), ('trip-result-worker', 'trip.result.apply'))
    groups = [[s['id'] for s in spans.values() if (s['service'], s['name']) == key]
              for key in required]
    missing = ['%s/%s' % key for key, group in zip(required, groups) if not group]
    def descends(child, ancestor):
        current = spans[child]['parent']
        while current in spans:
            if current == ancestor:
                return True
            current = spans[current]['parent']
        return False
    # Allow intermediate HTTP/database spans. Missing intermediate spans must not look complete.
    frontier = groups[0]
    for children in groups[1:]:
        frontier = [child for child in children if any(descends(child, parent) for parent in frontier)]
    complete = bool(frontier) and not missing
    return {'status': 'connected' if complete else 'partial', 'traceId': tid,
            'tracePathComplete': complete, 'missingBoundaries': missing,
            'boundaryCounts': {'%s/%s' % key: len(group) for key, group in zip(required, groups)},
            'spanCount': len(spans), 'spans': list(spans.values()),
            'limitation': 'One observed parent path, not loss/duplicate or latency evidence; compare business data separately'}
