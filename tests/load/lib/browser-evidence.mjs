// CDP evidence allowlist: no cookies, request headers, bodies or URL query strings.
export function canCapturePage(observed, attempted) {
  const origin='https://v1-staging.yeodam-2gether.com';
  if (new URL(observed).origin === origin) return true;
  return Boolean(attempted && new URL(attempted).origin === origin &&
    (observed === 'about:blank' || observed.startsWith('chrome-error://chromewebdata/')));
}

export function safeUrl(value) {
  try {
    const u = new URL(value);
    if (!['http:', 'https:'].includes(u.protocol)) return u.protocol + '[omitted]';
    const path = u.hostname.includes('amazonaws.com') ? '/[object]' : u.pathname;
    return `${u.protocol}//${u.host}${path}`;
  } catch { return '[unavailable]'; }
}

export function safeText(value) {
  return String(value).replace(/https?:\/\/[^\s"'<>]+/g, x => safeUrl(x))
    .replace(/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, '[jwt]')
    .replace(/(?:Bearer\s+)[^\s]+/gi, 'Bearer [redacted]')
    .replace(/((?:accessToken|refreshToken|csrfToken|CSRF_CONTEXT|authorization|cookie)\s*[:=]\s*)[^\s,;]+/gi, '$1[redacted]')
    .slice(0, 1200);
}

// Keep ambiguous pre-HTTP timeouts ambiguous; TCP metadata cannot decode TLS.
export function failureStage(r) {
  if (r.canceled) return 'client-canceled';
  if (r.blockedReason || r.corsError) return 'browser-policy';
  if (r.status >= 400) return 'http';
  if (r.finished !== false) return r.finished ? 'completed' : 'pending';
  const error = r.error || '';
  if (/NAME_NOT_RESOLVED|DNS_/i.test(error)) return 'dns';
  if (/CERT_|SSL_|TLS_/i.test(error)) return 'tls';
  if (/CONNECTION_REFUSED|ADDRESS_UNREACHABLE|NETWORK_UNREACHABLE/i.test(error)) return 'tcp';
  if (r.status || (r.timing?.ssl ?? -1) >= 0) return 'http-transport';
  return 'transport-stage-unknown';
}

export function sanitizeEvents(events) {
  const requests = new Map();const consoleEvents = [];let unmatched = 0;
  const interval = (t, begin, end) => t && t[begin] >= 0 && t[end] >= t[begin] ? t[end]-t[begin] : -1;
  for (const e of events) {
    const p = e.params || {};
    const key = `${e.sessionId || ''}:${p.requestId}`;
    if (e.method === 'Network.requestWillBeSent') {
      const previous = requests.get(key);
      if (previous) requests.set(key + ':redirect:' + requests.size, previous);
      requests.set(key, { id: p.requestId, url: safeUrl(p.request.url), method: p.request.method,
        resourceType: p.type, startedAt: p.wallTime ? new Date(p.wallTime*1000).toISOString() : null,
        timestamp: p.timestamp, initiatorType: p.initiator?.type || null });
    } else if (e.method === 'Network.responseReceived') {
      const r = requests.get(key);
      if (!r) { unmatched++;continue; }
      const v = p.response;
      const headers = Object.entries(v.headers || {});
      const requestId = headers.find(([name]) => name.toLowerCase() === 'x-request-id')?.[1] || null;
      const security = v.securityDetails;
      Object.assign(r, { status: v.status, protocol: v.protocol, mimeType: v.mimeType,
        connectionReused: v.connectionReused, connectionId: v.connectionId,
        fromDiskCache: v.fromDiskCache || false, fromServiceWorker: v.fromServiceWorker || false,
        requestId: typeof requestId === 'string' ? requestId.slice(0, 80) : null,
        timing: v.timing ? { requestTime: v.timing.requestTime,
          dns: interval(v.timing, 'dnsStart', 'dnsEnd'),
          connect: interval(v.timing, 'connectStart', 'connectEnd'),
          ssl: interval(v.timing, 'sslStart', 'sslEnd'),
          send: interval(v.timing, 'sendStart', 'sendEnd'),
          wait: interval(v.timing, 'sendEnd', 'receiveHeadersStart') } : null,
        tls: security ? { protocol: security.protocol, cipher: security.cipher,
          subjectName: security.subjectName, validFrom: security.validFrom, validTo: security.validTo } : null });
    } else if (e.method === 'Network.loadingFinished' || e.method === 'Network.loadingFailed') {
      const r = requests.get(key);
      if (!r) { unmatched++;continue; }
      Object.assign(r, { finished: e.method === 'Network.loadingFinished',
        durationMs: Math.max(0, (p.timestamp-r.timestamp)*1000),
        error: p.errorText ? safeText(p.errorText) : null, canceled: p.canceled || false,
        blockedReason: p.blockedReason || null, corsError: p.corsErrorStatus?.corsError || null });
    } else if (e.method === 'Runtime.consoleAPICalled') {
      consoleEvents.push({ type: p.type, timestamp: p.timestamp,
        messages: (p.args || []).map(x => x.type === 'string' ? safeText(x.value)
          : x.subtype === 'error' ? safeText((x.description || '').split('\n')[0]) : `[${x.type}]`) });
    } else if (e.method === 'Runtime.exceptionThrown') {
      consoleEvents.push({ type: 'exception', timestamp: p.timestamp,
        messages: [safeText(p.exceptionDetails?.text || ''), safeText((p.exceptionDetails?.exception?.description || '').split('\n')[0])] });
    }
  }
  return { requests: [...requests.values()], console: consoleEvents, unmatchedEvents: unmatched,
    privacy: { requestHeaders: false, responseHeaderAllowlist: ['x-request-id'], bodies: false, cookies: false,
      urlQueries: false, thirdPartyObjectPaths: false },
    scope: 'Browser CDP metadata, not a complete raw HAR; pending and canceled requests are not server failures' };
}

export function har(report) {
  return { log: { version: '1.2', creator: { name: 'yeodam-redacted-cdp', version: '1' },
    _privacy: report.privacy, entries: report.requests.filter(r => r.startedAt).map(r => ({
      startedDateTime: r.startedAt, time: r.durationMs ?? 0,
      request: { method: r.method, url: r.url, httpVersion: r.protocol || '', headers: [], cookies: [],
        queryString: [], headersSize: -1, bodySize: -1 },
      response: { status: r.status || 0, statusText: '', httpVersion: r.protocol || '',
        headers: r.requestId ? [{ name: 'x-request-id', value: r.requestId }] : [], cookies: [],
        content: { size: -1, mimeType: r.mimeType || '' }, redirectURL: '', headersSize: -1, bodySize: -1 },
      cache: {}, timings: { blocked: -1, dns: r.timing?.dns ?? -1, connect: r.timing?.connect ?? -1,
        ssl: r.timing?.ssl ?? -1, send: r.timing?.send ?? -1, wait: r.timing?.wait ?? -1, receive: -1 },
      _finished: r.finished ?? null, _error: r.error || null, _canceled: r.canceled || false,
      _durationUnavailable: r.durationMs === undefined,
    })) } };
}
