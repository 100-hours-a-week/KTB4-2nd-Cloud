// #438: unauthenticated HTTP pressure only; no sessions, writes or AI jobs.
import http from 'k6/http';
import exec from 'k6/execution';

const origin = 'https://v1-staging.yeodam-2gether.com';
const rate = Number(__ENV.K6_EDGE_RPS);
const seconds = Number(__ENV.K6_EDGE_SECONDS);
if (__ENV.K6_EDGE_EXECUTE !== '1' || __ENV.K6_EDGE_CONFIRM_TARGET !== origin ||
    !Number.isInteger(rate) || rate < 1 || rate > 500 ||
    !Number.isInteger(seconds) || seconds < 15 || seconds > 30) {
  throw new Error('Exact staging opt-in, 1..500 RPS and 15..30 seconds required');
}

export const options = {
  discardResponseBodies: true,
  maxRedirects: 0,
  scenarios: { replay: { executor: 'ramping-arrival-rate', startRate: Math.min(20, rate), timeUnit: '1s',
    stages: [{ duration: '5s', target: Math.min(20, rate) },
      { duration: '5s', target: rate }, { duration: `${seconds - 10}s`, target: rate }],
    preAllocatedVUs: 64, maxVUs: 64, gracefulStop: '5s' } },
  thresholds: { dropped_iterations: ['count==0'], http_req_failed: ['rate==0'] },
};
// V1 rejects an unauthenticated, CSRF-less POST with 403. This is a negative
// workload response, never a normal-user success.
http.setResponseCallback(http.expectedStatuses(400, 401, 403, 429));
export default function () {
  const r = http.post(`${origin}/api/trips`, '{}', {
    timeout: '3s', headers: { 'Content-Type': 'application/json',
      'User-Agent': 'yeodam-edge438' }, tags: { name: 'Unauthenticated POST /api/trips', cohort: 'replay' },
  });
  if (![400, 401, 403, 429].includes(r.status)) {
    exec.test.abort(`Unexpected replay response: ${r.status}`);
  }
}
