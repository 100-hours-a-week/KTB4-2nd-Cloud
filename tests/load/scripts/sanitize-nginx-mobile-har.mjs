#!/usr/bin/env node
// Offline iPhone Web Inspector export. Never retain cookies, bodies or query strings.
import fs from 'node:fs/promises';
import crypto from 'node:crypto';
import { pathToFileURL } from 'node:url';

const origin = 'https://v1-staging.yeodam-2gether.com';
const timingKeys = ['blocked', 'dns', 'connect', 'ssl', 'send', 'wait', 'receive'];
const finite = value => typeof value === 'number' && Number.isFinite(value) ? value : null;
const utc = value => typeof value === 'string' && /(Z|[+-]\d{2}:\d{2})$/.test(value) &&
  Number.isFinite(Date.parse(value)) ? value : null;

const providers = new Set(['https://accounts.kakao.com', 'https://kauth.kakao.com']);
const exactPaths = new Set(['/', '/login', '/trips', '/trips/create', '/mypage', '/auth/callback',
  '/api/actuator/health', '/api/users/me', '/api/trips', '/api/trips/map',
  '/api/auth/kakao/authorize', '/api/auth/kakao/callback', '/api/auth/csrf',
  '/api/auth/token/exchange', '/api/auth/token/refresh', '/api/auth/logout']);
function location(value, base) {
  try {
    if (!value || typeof value !== 'string') return null;
    const url = new URL(value, base);
    if (url.origin === origin) {
      let path = exactPaths.has(url.pathname) ? url.pathname : '/[other]';
      if (url.pathname.startsWith('/_next/')) path = '/_next/[asset]';
      if (/^\/api\/trips\/\d+(\/.*)?$/.test(url.pathname)) path = '/api/trips/:tripId/[route]';
      if (/^\/trips\/\d+(\/.*)?$/.test(url.pathname)) path = '/trips/:tripId/[route]';
      return origin + path;
    }
    if (providers.has(url.origin)) {
      const paths = new Set(['/login', '/login/', '/oauth/authorize', '/oauth/authorize/',
        '/oauth/authorize/decision', '/oauth/authorize/decision/', '/kakao_accounts/sign_in']);
      return url.origin + (paths.has(url.pathname) ? url.pathname : '/[other]');
    }
    return null;
  } catch { return null; }
}

export function failureStage(code) {
  if (!code) return null;
  if (/NAME_NOT_RESOLVED|NAME_RESOLUTION_FAILED/.test(code)) return 'dns';
  if (/CONNECTION_REFUSED|ADDRESS_UNREACHABLE/.test(code)) return 'tcp-connect';
  if (/SSL_|CERT_|TLS_/.test(code)) return 'tls';
  if (/BLOCKED_BY_|CORS/.test(code)) return 'browser-policy';
  if (/ABORTED/.test(code)) return 'client-canceled';
  return 'transport-stage-unknown';
}

export function sanitize(har) {
  const entries = har?.log?.entries;
  if (!Array.isArray(entries) || entries.length > 10000) throw new Error('Invalid/bounded HAR entries required');
  const requests = []; let omitted = 0;
  for (const entry of entries) {
    const url = location(entry?.request?.url);
    if (!url) { omitted++; continue; }
    const response = entry.response || {};
    const headers = Array.isArray(response.headers) ? response.headers : [];
    const rid = headers.find(x => String(x.name).toLowerCase() === 'x-request-id')?.value;
    const status = Number.isInteger(response.status) && response.status >= 0 && response.status <= 599 ? response.status : null;
    const error = String(entry._error || '').match(/\b(?:net::)?ERR_[A-Z0-9_]+\b/)?.[0] || null;
    const redirect = location(response.redirectURL || headers.find(x => String(x.name).toLowerCase() === 'location')?.value, url);
    requests.push({
      startedAt: utc(entry.startedDateTime), url,
      role: url.startsWith(origin + '/') ? 'service' : 'oauth-provider',
      method: /^(GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS)$/.test(entry.request.method) ? entry.request.method : null,
      status, protocol: /^(HTTP\/[0-9.]+|h2|h3)$/i.test(response.httpVersion || '') ? response.httpVersion : null,
      requestId: typeof rid === 'string' && /^[a-f0-9]{32}$/i.test(rid) ? rid : null,
      totalMs: finite(entry.time),
      timingsMs: Object.fromEntries(timingKeys.map(key => [key, finite(entry.timings?.[key])])),
      redirectWithinTarget: redirect?.startsWith(origin + '/') ? redirect : null,
      redirectTo: redirect, networkFailureCode: error, failureStage: failureStage(error),
      fetchType: ['Network Load', 'Disk Cache', 'Memory Cache'].includes(entry._fetchType) ? entry._fetchType : null,
      outcome: status === null || status === 0 ? 'no-http-response-recorded' : 'http-response-recorded',
    });
  }
  const present = fragment => requests.some(x => x.url === origin + fragment);
  const coverage = { authorize: present('/api/auth/kakao/authorize'),
    provider: requests.some(x => x.role === 'oauth-provider'), callback: present('/api/auth/kakao/callback'),
    exchange: present('/api/auth/token/exchange'), map: present('/api/trips/map') };
  return { version: 2, origin, sourceEntryCount: entries.length, retained: requests.length, omittedOutsideTarget: omitted,
    coverage, missingAuthPhases: Object.keys(coverage).filter(k => !coverage[k]),
    requests, privacy: { requestHeaders: false, cookies: false, bodies: false, queryStrings: false,
      thirdPartyUrls: 'Kakao OAuth origin and fixed route allowlist only', sourceIp: false, responseHeaderAllowlist: ['x-request-id'] },
    scope: 'Exported HAR metadata only. Status 0, missing timings and absent entries do not locate a DNS/TCP/TLS failure. Connect may include SSL; do not add them.' };
}

async function main() {
  const [input, output] = process.argv.slice(2);
  if (!input?.startsWith('/') || !output?.startsWith('/') || process.argv.length !== 4 || input === output)
    throw new Error('Usage: node sanitize-nginx-mobile-har.mjs /absolute/private.har /absolute/new-redacted.json');
  if ((await fs.stat(input)).size > 50 * 1024 * 1024) throw new Error('HAR exceeds 50 MiB; no partial export');
  const bytes = await fs.readFile(input);
  const result = { ...sanitize(JSON.parse(bytes)), sourceSha256: crypto.createHash('sha256').update(bytes).digest('hex'),
    sanitizedAt: new Date().toISOString() };
  await fs.writeFile(output, JSON.stringify(result, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
  console.log(JSON.stringify({ retained: result.retained, omitted: result.omittedOutsideTarget, outputCreated: true }));
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href)
  main().catch(() => { console.error('HAR export refused; check input bounds/JSON and a new writable output path.'); process.exitCode = 1; });
