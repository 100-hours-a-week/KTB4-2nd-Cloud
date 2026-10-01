import { check, fail } from 'k6';
import http from 'k6/http';

import { issueCsrfToken } from './api.mjs';
import { businessFailures } from './metrics.mjs';

let refreshSeeded = false;
let lastRefreshAt = 0;

function requestId(response) {
  for (const [name, value] of Object.entries(response.headers || {})) {
    if (name.toLowerCase() === 'x-request-id') return value;
  }
  return null;
}

function requireOk(response, operation) {
  const ok = check(response, { [`${operation}: status 200`]: (result) => result.status === 200 });
  if (!ok) {
    businessFailures.add(1, { operation });
    fail(`${operation}: 실제 status=${response.status}, request_id=${requestId(response)}`);
  }
}

export function initializeRefreshableSession(baseUrl, refreshToken) {
  const refreshUrl = `${baseUrl}/api/auth/token/refresh`;
  const jar = http.cookieJar();
  if (!refreshSeeded) {
    jar.set(refreshUrl, 'refreshToken', refreshToken, {
      path: '/api/auth',
      secure: true,
    });
    refreshSeeded = true;
  }
  const csrf = issueCsrfToken(baseUrl);
  const currentRefreshToken = jar.cookiesForURL(refreshUrl).refreshToken?.[0];
  if (!currentRefreshToken) fail('token_refresh: Refresh Cookie가 k6 Cookie Jar에 없습니다.');
  const response = http.post(refreshUrl, null, {
    headers: { 'X-CSRF-TOKEN': csrf },
    cookies: { refreshToken: { value: currentRefreshToken, replace: true } },
    responseType: 'none',
    tags: { endpoint: 'auth_token_refresh', operation: 'token_refresh' },
    timeout: '30s',
  });
  requireOk(response, 'token_refresh');
  lastRefreshAt = Date.now();

  const userResponse = http.get(`${baseUrl}/api/users/me`, {
    responseType: 'none',
    tags: { endpoint: 'users_me', operation: 'auth_check' },
    timeout: '30s',
  });
  requireOk(userResponse, 'auth_check');
  return requestId(userResponse);
}

export function refreshIfDue(baseUrl, refreshToken) {
  if (Date.now() - lastRefreshAt >= 20 * 60 * 1000) {
    initializeRefreshableSession(baseUrl, refreshToken);
  }
}
