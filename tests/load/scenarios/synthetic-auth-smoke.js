import { check, fail } from 'k6';
import http from 'k6/http';

import { normalizeBaseUrl, positiveIntegerEnv, requiredEnv } from '../lib/config.mjs';
import { summaryOutput } from '../lib/summary.mjs';

const pool = JSON.parse(open(requiredEnv('K6_SESSION_POOL_FILE')));
const baseUrl = normalizeBaseUrl(requiredEnv('K6_BASE_URL'));
const vus = positiveIntegerEnv('K6_SYNTHETIC_VUS', 100);

if (pool.version !== 1 || pool.baseUrl !== baseUrl || !Array.isArray(pool.sessions) || pool.sessions.length < vus) {
  throw new Error('Session pool version, URL, or VU count does not match.');
}
if (Date.parse(pool.expiresAt) <= Date.now() + 60_000) {
  throw new Error('Session pool Access Tokens have expired or have less than 60 seconds remaining.');
}

export const options = {
  scenarios: {
    synthetic_auth_smoke: {
      executor: 'per-vu-iterations', vus, iterations: 1, maxDuration: '3m', exec: 'authSmoke',
    },
  },
  thresholds: {
    checks: ['rate==1'],
    http_req_failed: ['rate==0'],
  },
};

export function authSmoke() {
  const session = pool.sessions[__VU - 1];
  const cookies = {
    accessToken: { value: session.accessToken },
    CSRF_CONTEXT: { value: session.csrfContext },
  };
  const user = http.get(`${baseUrl}/api/users/me`, {
    cookies, responseType: 'none', tags: { operation: 'synthetic_auth' }, timeout: '15s',
  });
  if (!check(user, { 'synthetic user authenticated': (response) => response.status === 200 })) {
    fail(`synthetic_auth failed: status=${user.status}, slot=${__VU}`);
  }
  const csrf = http.get(`${baseUrl}/api/auth/csrf`, {
    cookies, responseType: 'text', tags: { operation: 'synthetic_csrf' }, timeout: '15s',
  });
  let tokenMatches = false;
  if (csrf.status === 200) {
    try {
      tokenMatches = csrf.json().data?.token === session.csrfToken;
    } catch (_error) {
      tokenMatches = false;
    }
  }
  if (!check(tokenMatches, { 'synthetic CSRF token matches': (value) => value })) {
    fail(`synthetic_csrf failed: status=${csrf.status}, slot=${__VU}`);
  }
}

export function handleSummary(data) {
  return summaryOutput(data);
}
