import { normalizeBaseUrl, requiredEnv } from '../lib/config.mjs';
import { businessFailures } from '../lib/metrics.mjs';
import { initializeRefreshableSession } from '../lib/refresh-session.mjs';
import { summaryOutput } from '../lib/summary.mjs';

const baseUrl = normalizeBaseUrl(requiredEnv('K6_BASE_URL'));
const refreshToken = requiredEnv('K6_AUTH_SMOKE_REFRESH_TOKEN');

export const options = {
  scenarios: {
    auth_smoke: {
      executor: 'shared-iterations', vus: 1, iterations: 1,
      maxDuration: '1m', exec: 'authSmoke',
    },
  },
  thresholds: {
    checks: ['rate==1'],
    http_req_failed: ['rate==0'],
    yeodam_business_failures: ['count==0'],
  },
};

export function authSmoke() {
  businessFailures.add(0, { operation: 'auth_smoke' });
  initializeRefreshableSession(baseUrl, refreshToken);
  console.log('auth_smoke=success');
}

export function handleSummary(data) {
  return summaryOutput(data);
}
