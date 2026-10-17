import http from 'k6/http';
import crypto from 'k6/crypto';
import { SharedArray } from 'k6/data';
import { Rate } from 'k6/metrics';
import { requiredEnv } from '../lib/config.mjs';
import { summaryOutput } from '../lib/summary.mjs';

const baseUrl = requiredEnv('K6_BASE_URL').replace(/\/+$/, '');
if (baseUrl !== 'https://v1-staging.yeodam-2gether.com') throw new Error('Use V1 staging only');
const runId = requiredEnv('K6_TRIP_RUN_ID');
if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(runId)) {
  throw new Error('Run ID must be a canonical lowercase UUID');
}
const datasetId = requiredEnv('K6_TRIP_DATASET_ID');
const pool = new SharedArray('existing-user-session-pool', () => {
  const value = JSON.parse(open(requiredEnv('K6_SESSION_POOL_FILE')));
  return [{ version: value.version, mode: value.mode, runId: value.runId,
    datasetId: value.datasetId, baseUrl: value.baseUrl, expiresAt: value.expiresAt }, ...value.sessions];
});
const expiry = Date.parse(pool[0].expiresAt);
if (pool[0].version !== 1 || pool[0].mode !== 'existing' || pool[0].runId !== runId ||
    pool[0].datasetId !== datasetId || pool[0].baseUrl !== baseUrl || pool.length < 2 ||
    !Number.isFinite(expiry) || expiry <= Date.now() + 90000 ||
    !Number.isInteger(pool[1].userId) || !pool[1].accessToken || !pool[1].csrfToken || !pool[1].csrfContext) {
  throw new Error('Existing-user pool identity, credentials, or remaining lifetime is invalid');
}

const confirmed = new Rate('trip_create_confirmed');
export const options = {
  tags: { kind: 'migration_trip_contract', testid: runId },
  scenarios: { trip_contract: { executor: 'shared-iterations', vus: 1, iterations: 1, maxDuration: '30s' } },
  thresholds: { trip_create_confirmed: ['rate==1'] },
};

export default function () {
  const session = pool[1];
  const tripName = `t${crypto.sha256(runId + ':0', 'hex').slice(0, 9)}`;
  const request = { tripName, startDate: '2025-01-01', endDate: '2025-01-03', regionCodes: ['50110'] };
  console.log(JSON.stringify({ source: 'migration-trip-create', event: 'started', runId,
    userId: session.userId, tripName, request }));
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
  console.log(JSON.stringify({ source: 'migration-trip-create', event: 'final', runId,
    userId: session.userId, tripName, status: response.status, tripId: data?.tripId ?? null,
    outcome: ok ? 'confirmed' : response.status === 0 ? 'unconfirmed' : 'not_confirmed' }));
}

export function handleSummary(data) { return summaryOutput(data); }
