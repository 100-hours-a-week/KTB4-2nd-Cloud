import http from 'k6/http';
import crypto from 'k6/crypto';
import exec from 'k6/execution';
import { SharedArray } from 'k6/data';
import { Counter, Rate, Trend } from 'k6/metrics';
import { requiredEnv, positiveIntegerEnv } from '../lib/config.mjs';
import { summaryOutput } from '../lib/summary.mjs';

const base = requiredEnv('K6_BASE_URL').replace(/\/+$/, '');
const runId = requiredEnv('K6_TRIP_RUN_ID');
const dataset = requiredEnv('K6_TRIP_DATASET_ID');
const rate = positiveIntegerEnv('K6_TRIP_RATE', 10);
const duration = positiveIntegerEnv('K6_TRIP_DURATION_SECONDS', 900);
const vus = ({ 10: 40, 30: 120, 50: 200 })[rate];
if (base !== 'https://v1-staging.yeodam-2gether.com' || !/^[0-9a-f-]{36}$/.test(runId) ||
    !vus || duration !== 900) throw new Error('B integration requires staging, canonical Run, 10/30/50 RPS and 900s');
const pool = new SharedArray('b376-sessions', () => {
  const p = JSON.parse(open(requiredEnv('K6_SESSION_POOL_FILE')));
  return [p, ...p.sessions];
});
if (pool[0].runId !== runId || pool[0].datasetId !== dataset || pool.length - 1 < vus + 10 ||
    Date.parse(pool[0].expiresAt) < Date.now() + 1080000) throw new Error('Pool identity, count or remaining lifetime invalid');
const admitted = new Rate('b_request_admitted');
const accepted = new Counter('b_request_202');
const authFailures = new Counter('b_refresh_failures');
const firstLatency = new Trend('b_first_response_ms', true);
let session = null;
let nextRefresh = 0;
export const options = {
  tags: { testid: runId, kind: 'migration_b_integration' },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(95)', 'p(99)'],
  scenarios: { trip_load: { executor: 'constant-arrival-rate', rate, timeUnit: '1s',
    duration: '900s', preAllocatedVUs: vus, maxVUs: vus, gracefulStop: '30s' } },
  thresholds: { b_request_admitted: ['rate==1'], dropped_iterations: ['count==0'] },
};
export function setup() {
  console.log(JSON.stringify({ source: 'migration-b376', event: 'run_started', runId,
    rate, duration, vus, startedAt: Date.now(), completionDeadlineSeconds: 30, pollSeconds: 5 }));
}
function credentials() {
  if (session === null) { session = { ...pool[__VU] }; nextRefresh = Date.now() + 60000; }
  if (Date.now() >= nextRefresh) {
    const r = http.post(`${base}/api/auth/token/refresh`, null, {
      cookies: { refreshToken: { value: session.refreshToken }, CSRF_CONTEXT: { value: session.csrfContext } },
      headers: { 'X-CSRF-TOKEN': session.csrfToken }, responseCallback: http.expectedStatuses(200),
      timeout: '10s', redirects: 0, tags: { operation: 'auth_refresh' },
    });
    const access = r.cookies?.accessToken?.[0]?.value;
    const refresh = r.cookies?.refreshToken?.[0]?.value;
    const ok = r.status === 200 && access && refresh;
    if (ok) { session.accessToken = access; session.refreshToken = refresh; }
    else authFailures.add(1);
    nextRefresh = Date.now() + 60000;
    console.log(JSON.stringify({ source: 'migration-b376', event: 'auth_refresh', runId, vu: __VU, status: r.status, ok: Boolean(ok) }));
  }
  return session;
}
export default function () {
  const iteration = exec.scenario.iterationInTest;
  const s = credentials();
  const hex = crypto.sha256(`${runId}:${iteration}`, 'hex').slice(0, 32);
  const requestId = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  const tripName = `t${crypto.sha256(runId, 'hex').slice(0, 5)}${iteration.toString(36).padStart(4, '0')}`;
  const payload = { tripName, startDate: '2025-01-01', endDate: '2025-01-03', regionCodes: ['50110'] };
  console.log(JSON.stringify({ source: 'migration-b376', event: 'started', runId, iteration, requestId, tripName, userId: s.userId, request: payload }));
  const t0 = Date.now();
  const params = { cookies: { accessToken: { value: s.accessToken }, CSRF_CONTEXT: { value: s.csrfContext } },
    headers: { 'Content-Type': 'application/json', 'X-CSRF-TOKEN': s.csrfToken, 'X-Request-ID': requestId },
    responseCallback: http.expectedStatuses(201, 202), timeout: '10s', redirects: 0, tags: { operation: 'trip_create' } };
  const r = http.post(`${base}/api/trips`, JSON.stringify(payload), params);
  let data = null;
  try { data = r.json()?.data; } catch (_) { /* Preserve ambiguous first responses. */ }
  const firstMs = Date.now() - t0;
  const isAccepted = r.status === 202 && data?.requestId === requestId;
  let ok = r.status === 201 && Number.isInteger(data?.tripId);
  let tripId = data?.tripId ?? null;
  admitted.add(ok || isAccepted); firstLatency.add(firstMs);
  console.log(JSON.stringify({ source: 'migration-b376', event: 'first_response', runId, requestId,
    iteration, tripName, userId: s.userId, startedAt: t0, status: r.status, tripId,
    accepted: isAccepted, admitted: ok || isAccepted, elapsedMs: firstMs,
    retryAfterSeconds: Math.max(1, Math.min(30, Number(r.headers?.['Retry-After'] || 5))) }));
  if (isAccepted) { accepted.add(1); return; }
  console.log(JSON.stringify({ source: 'migration-b376', event: 'final', runId, iteration, requestId,
    tripName, userId: s.userId, status: r.status, tripId,
    outcome: ok ? 'confirmed' : 'not_confirmed', elapsedMs: Date.now() - t0 }));
}
export function handleSummary(data) { return summaryOutput(data); }
