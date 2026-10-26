import http from 'k6/http';
import crypto from 'k6/crypto';
import exec from 'k6/execution';
import { SharedArray } from 'k6/data';
import { Counter, Rate, Trend } from 'k6/metrics';
import { createConfig, validatePool } from '../lib/nginx-create.mjs';
import { summaryOutput } from '../lib/summary.mjs';

const c = createConfig(__ENV);
const pools = new SharedArray('nginx428-private-pools', () => [
  validatePool(JSON.parse(open(__ENV.K6_SESSION_POOL_FILE)), c.seconds),
  validatePool(JSON.parse(open(__ENV.K6_READ_POOL_FILE)), c.seconds),
]);
if (pools[0].sessions.length !== 3 || pools[1].sessions.length !== 1 || pools[1].sessions[0].userId !== 11007) {
  throw new Error('Use three new synthetic owners and the separate fixed read fixture');
}
const started = new Counter('create_started');
const confirmed = new Rate('create_confirmed');
const normalOk = new Rate('normal_read_ok');
const elapsed = new Trend('create_elapsed_ms', true);
const readElapsed = new Trend('normal_read_elapsed_ms', true);
export const options = {
  noConnectionReuse: c.noReuse, maxRedirects: 0,
  tags: { testid: c.runId, kind: 'nginx_create_428' },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(95)', 'p(99)'],
  scenarios: {
    create: c.mode === 'rate' ? {
      executor: 'constant-arrival-rate', exec: 'create', rate: c.rate, timeUnit: '1s',
      duration: `${c.seconds}s`, preAllocatedVUs: c.fixedVus, maxVUs: c.fixedVus, gracefulStop: '12s',
    } : { executor: 'shared-iterations', exec: 'create', vus: c.mode === 'burst' ? 3 : 1,
      iterations: c.mode === 'burst' ? 3 : 1, maxDuration: '20s' },
    normal: { executor: 'constant-arrival-rate', exec: 'normal', rate: 1, timeUnit: '2s',
      duration: c.mode === 'rate' ? `${c.seconds}s` : '4s', preAllocatedVUs: 2, maxVUs: 2, gracefulStop: '5s' },
  },
  thresholds: {
    create_confirmed: ['rate==1'], normal_read_ok: ['rate==1'],
    dropped_iterations: [{ threshold: 'count==0', abortOnFail: true, delayAbortEval: '5s' }],
  },
};

function params(s, operation) {
  return { cookies: { accessToken: { value: s.accessToken }, CSRF_CONTEXT: { value: s.csrfContext } },
    headers: { 'Content-Type': 'application/json', 'X-CSRF-TOKEN': s.csrfToken },
    redirects: 0, timeout: '10s', tags: { operation }, responseCallback: http.expectedStatuses(operation === 'trip_create' ? 201 : 200) };
}
function rid(r) { return Object.entries(r.headers || {}).find(([k]) => k.toLowerCase() === 'x-request-id')?.[1] || null; }
function event(value) { console.log(JSON.stringify({ source: 'nginx-create-428', runId: c.runId, ...value })); }

export function setup() {
  event({ event: 'run_started', utc: new Date().toISOString(), mode: c.mode, rate: c.rate,
    seconds: c.seconds, sessionCount: c.sessionCount, fixedVus: c.fixedVus, noReuse: c.noReuse,
    payload: { regionCodes: ['50110'], dates: ['2025-01-01', '2025-01-03'] },
    noPhotoUploadOrAI: true, guard: 'first non201/non200, read >1s, create >1s or dropped iteration aborts; not a service SLO' });
}
export function create() {
  const i = exec.scenario.iterationInTest;
  const s = pools[0].sessions[i % c.sessionCount];
  const tripName = `c${crypto.sha256(c.runId, 'hex').slice(0, 5)}${i.toString(36).padStart(4, '0')}`;
  if (tripName.length !== 10 || Date.now() >= Date.parse(pools[0].expiresAt) - 30000) exec.test.abort('Name space or token guard');
  const at = Date.now(); started.add(1);
  event({ event: 'started', iteration: i, userId: s.userId, tripName, atMs: at });
  const r = http.post(c.origin + '/api/trips', JSON.stringify({ tripName, startDate: '2025-01-01', endDate: '2025-01-03', regionCodes: ['50110'] }), params(s, 'trip_create'));
  let tripId = null; try { tripId = r.json()?.data?.tripId; } catch (_) { /* Keep ambiguous requests for DB reconciliation. */ }
  const ms = Date.now() - at, ok = r.status === 201 && Number.isInteger(tripId);
  confirmed.add(ok); elapsed.add(ms);
  event({ event: 'final', iteration: i, userId: s.userId, tripName, status: r.status, tripId,
    atMs: Date.now(), elapsedMs: ms, httpDurationMs: r.timings.duration, requestId: rid(r), protocol: r.proto,
    outcome: ok ? 'confirmed' : 'unconfirmed' });
  if (!ok || ms > 1000 || r.proto !== 'HTTP/2.0') exec.test.abort('Create response, initial one-second or protocol guard; preserve data');
}
export function normal() {
  const s = pools[1].sessions[0];
  const routes = ['/api/trips/map', '/api/trips', '/api/trips/2048046'];
  const path = routes[exec.scenario.iterationInTest % routes.length], at = Date.now();
  const r = http.get(c.origin + path, params(s, 'normal_read'));
  const ms = Date.now() - at; normalOk.add(r.status === 200 && ms <= 1000); readElapsed.add(ms);
  event({ event: 'normal', route: path, status: r.status, atMs: Date.now(), elapsedMs: ms, requestId: rid(r) });
  if (r.status !== 200 || ms > 1000) exec.test.abort('Normal user read guard; stop stage');
}
export function teardown() { event({ event: 'run_finished', utc: new Date().toISOString() }); }
export function handleSummary(data) { return summaryOutput(data); }
