import {
  deleteTrip,
  initializeAuthenticatedSession,
  issueCsrfToken,
} from '../lib/api.mjs';
import { normalizeBaseUrl, positiveIntegerEnv, requiredEnv } from '../lib/config.mjs';
import { businessFailures } from '../lib/metrics.mjs';
import { emitRunEvent, summaryOutput } from '../lib/summary.mjs';

const baseUrl = normalizeBaseUrl(requiredEnv('K6_BASE_URL'));
const accessToken = requiredEnv('K6_ACCESS_TOKEN');
const tripId = positiveIntegerEnv('K6_TRIP_ID', 0);

export const options = {
  discardResponseBodies: true,
  scenarios: {
    cleanup_trip: {
      executor: 'shared-iterations',
      vus: 1,
      iterations: 1,
      maxDuration: '2m',
      exec: 'cleanupTrip',
      tags: { test_scope: 'cleanup', workload: 'trip_delete' },
    },
  },
  thresholds: {
    checks: ['rate==1'],
    http_req_failed: ['rate==0'],
    yeodam_business_failures: ['count==0'],
  },
};

export function cleanupTrip() {
  businessFailures.add(0, { operation: 'trip_delete' });
  initializeAuthenticatedSession(baseUrl, accessToken);
  const csrfToken = issueCsrfToken(baseUrl);
  const requestId = deleteTrip(baseUrl, csrfToken, tripId);
  emitRunEvent({
    event: 'cleanup_completed',
    scenario: 'cleanup_trip',
    endedAt: new Date().toISOString(),
    tripId,
    requestId,
  });
}

export function handleSummary(data) {
  return summaryOutput(data);
}
