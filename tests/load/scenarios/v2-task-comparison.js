import http from 'k6/http';
import exec from 'k6/execution';
import { SharedArray } from 'k6/data';
import { Counter, Trend } from 'k6/metrics';
import { summaryOutput } from '../lib/summary.mjs';

const base = 'https://staging.yeodam-2gether.com';
if (__ENV.K6_BASE_URL !== base || __ENV.K6_HTTP_DEBUG) throw new Error('Staging only; HTTP debug forbidden');
const cohort = __ENV.K6_TASK_COHORT;
if (!['single', 'double'].includes(cohort)) throw new Error('Specify single or double');
const limitRate = Number(__ENV.K6_TASK_LIMIT_RATE || 0);
const limitSeconds = Number(__ENV.K6_TASK_LIMIT_SECONDS || 120);
if (limitRate && (!Number.isInteger(limitRate) || limitRate < 50 || limitRate > 6400 ||
    ![120, 180].includes(limitSeconds))) throw new Error('Invalid capacity stage');
const pool = new SharedArray('v2-task-comparison-pool', () => {
  const data = JSON.parse(open(__ENV.K6_SESSION_POOL_FILE));
  if (data.baseUrl !== base || data.issue !== 474 || data.sessions.length !== 100 ||
      Date.parse(data.expiresAt) < Date.now() + 600000) throw new Error('Invalid or short-lived pool');
  return data.sessions;
});
const failures = new Counter('business_failures');
const latency = new Trend('business_duration', true);
export const options = {
  discardResponseBodies: true,
  noConnectionReuse: false,
  noVUConnectionReuse: false,
  summaryTrendStats: ['avg', 'med', 'p(95)', 'p(99)', 'max'],
  tags: { issue: '474', cohort },
  scenarios: Object.fromEntries((limitRate ? [
    ['warmup', 10, '0s', '30s'], [`r${limitRate}`, limitRate, '40s', `${limitSeconds}s`],
  ] : [
    ['warmup', 10, '0s', '30s'], ['r10', 10, '40s', '120s'],
    ['r30', 30, '170s', '120s'], ['r50', 50, '300s', '120s'],
  ]).map(([name, rate, startTime, duration]) => [name, {
    executor: 'constant-arrival-rate', rate, timeUnit: '1s', startTime, duration,
    preAllocatedVUs: limitRate && name !== 'warmup' ? 1000 : 100,
    maxVUs: limitRate && name !== 'warmup' ? 1000 : 100,
    gracefulStop: limitRate ? '30s' : '10s',
    tags: { phase: name, rate: String(rate) },
  }])),
  thresholds: {
    business_failures: ['count==0'], dropped_iterations: ['count==0'],
    http_req_failed: ['rate==0'],
  },
};

export default function () {
  // Each arrival makes exactly one business request. No per-VU login/CSRF traffic.
  const slot = exec.scenario.iterationInTest % pool.length;
  const session = pool[slot];
  const write = exec.scenario.iterationInTest % 5 === 0;
  const name = `${exec.scenario.name.slice(-2)}${String(exec.scenario.iterationInTest).padStart(8, '0')}`;
  const params = {
    cookies: { accessToken: session.accessToken, CSRF_CONTEXT: session.csrfContext },
    headers: { 'Content-Type': 'application/json', 'X-CSRF-TOKEN': session.csrfToken },
    timeout: '30s', responseType: write ? 'text' : 'none',
    tags: { operation: write ? 'trip_create' : 'trip_list' },
  };
  if (write && limitRate) console.log(JSON.stringify({ event: 'write_started', cohort,
    phase: exec.scenario.name, iteration: exec.scenario.iterationInTest,
    userId: session.userId, tripName: name }));
  const response = write
    ? http.post(`${base}/api/trips`, JSON.stringify({ tripName: name,
      startDate: '2026-01-01', endDate: '2026-01-03', regionCodes: ['50110'] }), params)
    : http.get(`${base}/api/trips`, params);
  latency.add(response.timings.duration, { operation: write ? 'trip_create' : 'trip_list' });
  let tripId = null;
  if (write && response.status === 201) {
    try { tripId = response.json().data.tripId; } catch (_) { /* counted below */ }
  }
  const ok = response.status === (write ? 201 : 200) && (!write || Number.isInteger(tripId));
  failures.add(ok ? 0 : 1);
  if (write || !ok) console.log(JSON.stringify({ event: 'result', cohort,
    phase: exec.scenario.name, iteration: exec.scenario.iterationInTest,
    userId: session.userId, operation: write ? 'trip_create' : 'trip_list',
    tripName: write ? name : null, status: response.status, tripId, ok }));
}

export function handleSummary(data) { return summaryOutput(data); }
