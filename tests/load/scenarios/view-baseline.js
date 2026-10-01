import { check, sleep } from 'k6';
import exec from 'k6/execution';

import { initializeAuthenticatedSession, viewTrip } from '../lib/api.mjs';
import {
  normalizeBaseUrl,
  optionalEnv,
  positiveIntegerEnv,
  requiredEnv,
} from '../lib/config.mjs';
import { businessFailures } from '../lib/metrics.mjs';
import { emitRunEvent, summaryOutput } from '../lib/summary.mjs';

const baseUrl = normalizeBaseUrl(requiredEnv('K6_BASE_URL'));
const accessToken = requiredEnv('K6_ACCESS_TOKEN');
const tripId = positiveIntegerEnv('K6_COMPLETED_TRIP_ID', 0);
const vus = positiveIntegerEnv('K6_VIEW_VUS', 1);
const duration = optionalEnv('K6_VIEW_DURATION', '5m');
const thinkTimeSeconds = positiveIntegerEnv('K6_VIEW_THINK_TIME_SECONDS', 2);

export const options = {
  discardResponseBodies: true,
  noCookiesReset: true,
  scenarios: {
    view_baseline: {
      executor: 'constant-vus',
      vus,
      duration,
      gracefulStop: '30s',
      exec: 'viewBaseline',
      tags: { test_scope: 'baseline', workload: 'trip_view' },
    },
  },
  thresholds: {
    checks: ['rate==1'],
    http_req_failed: ['rate<0.01'],
    yeodam_business_failures: ['count==0'],
    yeodam_trip_view_duration: ['p(95)<1000'],
  },
};

let authenticated = false;

export function viewBaseline() {
  businessFailures.add(0, { operation: 'trip_view_result' });
  try {
    if (!authenticated) {
      initializeAuthenticatedSession(baseUrl, accessToken);
      authenticated = true;
    }

    const viewed = viewTrip(baseUrl, tripId);
    const valid = check(viewed.detail, {
      'trip_view: requested trip returned': (detail) => detail && detail.tripId === tripId,
    });
    if (!valid) businessFailures.add(1, { operation: 'trip_view_result' });
  } catch (error) {
    if (String(error).includes('실제 status=401')) {
      exec.test.abort(`view_baseline: 인증 만료로 중단했습니다. ${error}`);
    }
    throw error;
  } finally {
    sleep(thinkTimeSeconds);
  }
}

export function setup() {
  emitRunEvent({
    event: 'run_started',
    scenario: 'view_baseline',
    startedAt: new Date().toISOString(),
    tripId,
    vus,
    duration,
    cloudRelease: optionalEnv('K6_CLOUD_RELEASE', 'unknown'),
  });
}

export function teardown() {
  emitRunEvent({
    event: 'run_completed',
    scenario: 'view_baseline',
    endedAt: new Date().toISOString(),
    tripId,
  });
}

export function handleSummary(data) {
  return summaryOutput(data);
}
