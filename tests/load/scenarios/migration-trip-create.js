import http from 'k6/http';
import crypto from 'k6/crypto';
import exec from 'k6/execution';
import { SharedArray } from 'k6/data';
import { Counter, Rate } from 'k6/metrics';
import { positiveIntegerEnv, requiredEnv } from '../lib/config.mjs';
import { summaryOutput } from '../lib/summary.mjs';

const baseUrl = requiredEnv('K6_BASE_URL').replace(/\/+$/, '');
if (baseUrl !== 'https://v1-staging.yeodam-2gether.com') throw new Error('Use V1 staging only');
const runId = requiredEnv('K6_TRIP_RUN_ID');
if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(runId)) {
  throw new Error('Run ID must be a canonical lowercase UUID');
}
const datasetId = requiredEnv('K6_TRIP_DATASET_ID');
const mode = __ENV.K6_TRIP_MODE || 'contract';
if (!['contract', 'load'].includes(mode)) throw new Error('Trip mode must be contract or load');
const rate = positiveIntegerEnv('K6_TRIP_RATE', 10);
const duration = positiveIntegerEnv('K6_TRIP_DURATION_SECONDS', 120);
const preVus = positiveIntegerEnv('K6_TRIP_PREALLOCATED_VUS', 40);
const maxVus = positiveIntegerEnv('K6_TRIP_MAX_VUS', preVus);
if (mode === 'load' && (rate !== 10 || duration !== 120 || maxVus < preVus || preVus < 40)) {
  throw new Error('This issue permits only 10 RPS for 120s with at least 40 preallocated VUs');
}
const pool = new SharedArray('existing-user-session-pool', () => {
  const value = JSON.parse(open(requiredEnv('K6_SESSION_POOL_FILE')));
  return [{ version: value.version, mode: value.mode, runId: value.runId,
    datasetId: value.datasetId, baseUrl: value.baseUrl, expiresAt: value.expiresAt }, ...value.sessions];
});
const expiry = Date.parse(pool[0].expiresAt);
if (pool[0].version !== 1 || pool[0].mode !== 'existing' || pool[0].runId !== runId ||
    pool[0].datasetId !== datasetId || pool[0].baseUrl !== baseUrl || pool.length < 2 ||
    !Number.isFinite(expiry) || expiry <= Date.now() + (mode === 'load' ? duration * 1000 : 0) + 90000) {
  throw new Error('Existing-user pool identity, credentials, or remaining lifetime is invalid');
}
for (let index = 1; index < pool.length; index += 1) {
  const session = pool[index];
  if (!Number.isInteger(session.userId) || !session.accessToken || !session.csrfToken || !session.csrfContext) {
    throw new Error('Existing-user pool has an invalid session');
  }
}

const confirmed = new Rate('trip_create_confirmed');
const started = new Counter('trip_create_started');
const finalFailures = new Counter('trip_create_final_failures');
export const options = {
  tags: { kind: mode === 'contract' ? 'migration_trip_contract' : 'migration_trip_load', testid: runId },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(95)', 'p(99)'],
  scenarios: mode === 'contract' ? {
    trip_contract: { executor: 'shared-iterations', vus: 1, iterations: 1, maxDuration: '30s' },
  } : {
    trip_load: { executor: 'constant-arrival-rate', rate, timeUnit: '1s', duration: `${duration}s`,
      preAllocatedVUs: preVus, maxVUs: maxVus, gracefulStop: '30s' },
  },
  thresholds: mode === 'contract' ? { trip_create_confirmed: ['rate==1'] } : {
    trip_create_confirmed: ['rate==1'], dropped_iterations: ['count==0'],
  },
};

export function setup() {
  console.log(JSON.stringify({ source: 'migration-trip-create', event: 'run_started', runId,
    mode, rate, duration, sessionCount: pool.length - 1, preVus, maxVus }));
}

export default function () {
  if (Date.now() >= expiry - 10000) exec.test.abort('Session pool expired; preserve events and reconcile');
  const iteration = exec.scenario.iterationInTest;
  const session = pool[1 + ((__VU - 1) % (pool.length - 1))];
  // The run prefix separates experiments; the base36 suffix is injective within this run.
  const suffix = iteration.toString(36).padStart(4, '0');
  if (suffix.length !== 4) exec.test.abort('Trip name space exhausted');
  const tripName = `t${crypto.sha256(runId, 'hex').slice(0, 5)}${suffix}`;
  const request = { tripName, startDate: '2025-01-01', endDate: '2025-01-03', regionCodes: ['50110'] };
  console.log(JSON.stringify({ source: 'migration-trip-create', event: 'started', runId,
    iteration, userId: session.userId, tripName, request }));
  started.add(1);
  const response = http.post(`${baseUrl}/api/trips`, JSON.stringify(request), {
    cookies: { accessToken: { value: session.accessToken },
      CSRF_CONTEXT: { value: session.csrfContext } },
    headers: { 'Content-Type': 'application/json', 'X-CSRF-TOKEN': session.csrfToken },
    responseCallback: http.expectedStatuses(201), timeout: '10s', redirects: 0,
    tags: { operation: 'trip_create' },
  });
  let data = null;
  try { data = response.json()?.data; } catch (_) { /* DB reconciliation decides an ambiguous response. */ }
  const ok = response.status === 201 && Number.isInteger(data?.tripId) &&
    data.status === 'PROCESSING';
  confirmed.add(ok);
  finalFailures.add(ok ? 0 : 1);
  console.log(JSON.stringify({ source: 'migration-trip-create', event: 'final', runId,
    iteration, userId: session.userId, tripName, status: response.status, tripId: data?.tripId ?? null,
    outcome: ok ? 'confirmed' : response.status === 0 ? 'unconfirmed' : 'not_confirmed' }));
  if ([400, 401, 403, 409].includes(response.status)) {
    exec.test.abort(`Trip request contract/auth failed with ${response.status}; reconcile recorded requests`);
  }
}

export function handleSummary(data) { return summaryOutput(data); }
