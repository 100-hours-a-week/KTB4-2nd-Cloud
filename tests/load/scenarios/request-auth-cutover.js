// Disposable #311 environment only. The user starts this test; it never changes routing.
import http from 'k6/http';
import crypto from 'k6/crypto';
import exec from 'k6/execution';
import { SharedArray } from 'k6/data';
import { Rate, Counter } from 'k6/metrics';

const base = __ENV.K6_CUTOVER_BASE_URL || 'https://localhost:19443';
if (base !== 'https://localhost:19443') throw new Error('Only the isolated local/SSM-forwarded TLS fixture is allowed');
const run = __ENV.K6_CUTOVER_RUN_ID;
if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(run || '')) {
  throw new Error('K6_CUTOVER_RUN_ID is required');
}
const data = new SharedArray('cutover-sessions', () => {
  const p = JSON.parse(open(__ENV.K6_CUTOVER_POOL_FILE));
  if (p.runId !== run || p.sessions.length < 5 || p.issuedAt * 1000 + 1800000 < Date.now() + 180000) {
    throw new Error('Fresh run-specific pool with >=5 sessions required');
  }
  return p.sessions.slice(4); // smoke uses the first identities for Refresh/Logout/OAuth checks.
});
const rate = Number(__ENV.K6_CUTOVER_RPS || 10);
if (![1, 10, 30, 100].includes(rate)) throw new Error('Use 1, 10, 30 or 100 RPS');
const ok = new Rate('cutover_confirmed');
const started = new Counter('cutover_started');
export const options = {
  scenarios: { continuity: { executor: 'constant-arrival-rate', rate, timeUnit: '1s',
    duration: '120s', preAllocatedVUs: Math.max(10, rate * 4), maxVUs: Math.max(10, rate * 4), gracefulStop: '20s' } },
  tags: { kind: 'request_auth_cutover', testid: run },
  summaryTrendStats: ['avg', 'med', 'p(95)', 'p(99)', 'max'],
  thresholds: { cutover_confirmed: ['rate==1'], dropped_iterations: ['count==0'] },
};
export default function () {
  const n = exec.scenario.iterationInTest;
  const session = data[(__VU - 1) % data.length];
  const id = `${run}_${n}`;
  const write = n % 10 === 0;
  const tripName = `c${crypto.sha256(run, 'hex').slice(0, 5)}${n.toString(36).padStart(4, '0')}`;
  const event = { source: 'request-auth-cutover-k6', run_id: run, request_id: id,
    operation: write ? 'trip_metadata' : 'existing_login', user_id: session.userId,
    trip_name: write ? tripName : null };
  console.log(JSON.stringify({ ...event, event: 'started' }));
  started.add(1);
  const params = { headers: { 'X-Request-ID': id, 'Content-Type': 'application/json',
    'X-CSRF-TOKEN': session.csrfToken }, cookies: { accessToken: session.accessToken,
    CSRF_CONTEXT: session.csrfContext }, timeout: '15s', redirects: 0,
    responseCallback: http.expectedStatuses(write ? 201 : 200) };
  const response = write ? http.post(base + '/api/trips', JSON.stringify({ tripName,
    startDate: '2025-01-01', endDate: '2025-01-03', regionCodes: ['50110'] }), params)
    : http.get(base + '/api/users/me', params);
  let tripId = null;
  if (write) { try { tripId = response.json()?.data?.tripId; } catch (_) { /* reconcile DB */ } }
  const confirmed = response.status === (write ? 201 : 200) && (!write || Number.isInteger(tripId));
  ok.add(confirmed);
  console.log(JSON.stringify({ ...event, event: 'final', status: response.status,
    trip_id: tripId, confirmed, target: response.headers['X-Yeodam-Cutover-Target'] || null,
    duration_ms: response.timings.duration }));
}
export function handleSummary(value) {
  return { [__ENV.K6_CUTOVER_SUMMARY || 'cutover-summary.json']: JSON.stringify(value, null, 2) };
}
