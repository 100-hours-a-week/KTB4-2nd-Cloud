import { sleep } from 'k6';
import http from 'k6/http';
import crypto from 'k6/crypto';
import exec from 'k6/execution';
import { SharedArray } from 'k6/data';
import { Counter, Rate, Trend } from 'k6/metrics';
import { positiveIntegerEnv, requiredEnv } from '../lib/config.mjs';
import { confirmedResponse, retryableStatus, uuidFromHex } from '../lib/migration-probe.mjs';
import { summaryOutput } from '../lib/summary.mjs';

const baseUrl = requiredEnv('K6_BASE_URL').replace(/\/+$/, '');
if (baseUrl !== 'https://v1-staging.yeodam-2gether.com') throw new Error('Use the V1 staging endpoint only');
const runId = requiredEnv('K6_PROBE_RUN_ID');
if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(runId)) throw new Error('Run ID must be a lowercase UUID');
const generatorId = requiredEnv('K6_PROBE_GENERATOR_ID');
if (!/^[a-zA-Z0-9_-]{1,32}$/.test(generatorId)) throw new Error('Invalid generator ID');
const mode = __ENV.K6_PROBE_MODE || 'load';
if (!['load', 'contract'].includes(mode)) throw new Error('Mode must be load or contract');
const rate = positiveIntegerEnv('K6_PROBE_RATE', 10);
const duration = positiveIntegerEnv('K6_PROBE_DURATION_SECONDS', 60);
const preVus = positiveIntegerEnv('K6_PROBE_PREALLOCATED_VUS', 10);
const maxVus = positiveIntegerEnv('K6_PROBE_MAX_VUS', preVus);
const retries = Number(__ENV.K6_PROBE_MAX_RETRIES || '2');
if (!Number.isInteger(retries) || retries < 0 || retries > 3) throw new Error('Max retries must be 0..3');
const timeoutMs = positiveIntegerEnv('K6_PROBE_TIMEOUT_MS', 2000);
const budgetMs = positiveIntegerEnv('K6_PROBE_RETRY_BUDGET_MS', 8000);
if (maxVus < preVus || budgetMs < timeoutMs || budgetMs > 60000) throw new Error('Invalid VU count or retry budget');
const pool = new SharedArray('migration-session-pool', () => {
  const value = JSON.parse(open(requiredEnv('K6_SESSION_POOL_FILE')));
  return [{ version: value.version, baseUrl: value.baseUrl, expiresAt: value.expiresAt }, ...value.sessions];
});
const expiresAt = Date.parse(pool[0].expiresAt);
if (pool[0].version !== 1 || pool[0].baseUrl !== baseUrl || pool.length < 2 ||
    !Number.isFinite(expiresAt) || expiresAt <= Date.now() + duration * 1000 + 90000) {
  throw new Error('Pool must contain sessions and remain valid for the complete test including grace time');
}
const started = new Counter('probe_started');
const confirmed = new Counter('probe_confirmed');
const finalFailures = new Counter('probe_final_failures');
const firstFailures = new Counter('probe_first_failures');
const retryCount = new Counter('probe_retries');
const committed = new Rate('probe_committed');
const endToEnd = new Trend('probe_end_to_end', true);
const contractPass = new Rate('probe_contract_pass');
const accepted = http.expectedStatuses(200, 201);

export const options = {
  tags: { kind: 'migration_probe', testid: runId, generator: generatorId },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(95)', 'p(99)'],
  scenarios: mode === 'contract' ? {
    probe_contract: { executor: 'shared-iterations', vus: 1, iterations: 1, exec: 'contract', maxDuration: '30s' },
  } : {
    probe_load: { executor: 'constant-arrival-rate', rate, timeUnit: '1s', duration: `${duration}s`,
      preAllocatedVUs: preVus, maxVUs: maxVus, gracefulStop: '30s', exec: 'writeProbe' },
  },
  thresholds: mode === 'contract' ? { probe_contract_pass: ['rate==1'] } : {
    probe_committed: ['rate==1'], dropped_iterations: ['count==0'],
  },
};

function emit(event) { console.log(JSON.stringify({ source: 'migration-probe', ...event })); }
function params(timeout = timeoutMs, callback = accepted) {
  const session = pool[1 + ((__VU - 1) % (pool.length - 1))];
  return {
    cookies: { accessToken: { value: session.accessToken }, CSRF_CONTEXT: { value: session.csrfContext } },
    headers: { 'Content-Type': 'application/json', 'X-CSRF-TOKEN': session.csrfToken },
    responseCallback: callback, timeout: `${timeout}ms`, redirects: 0,
    tags: { operation: 'migration_write' },
  };
}
function send(request, timeout = timeoutMs, callback = accepted) {
  const p = params(timeout, callback);
  p.headers['X-Request-ID'] = request.requestId;
  p.headers['X-Migration-Run-ID'] = request.runId;
  return http.post(`${baseUrl}/api/internal/migration-write-probe`, JSON.stringify(request), p);
}
function body(response) { try { return response.json(); } catch (_) { return null; } }
function requestForIteration() {
  const key = `${runId}:${generatorId}:${exec.scenario.iterationInTest}`;
  return { runId, requestId: uuidFromHex(crypto.sha256(key, 'hex')), payloadHash: crypto.sha256(`payload:${key}`, 'hex') };
}
export function setup() {
  emit({ event: 'run_started', runId, generatorId, mode, rate, duration, maxVus, sessionCount: pool.length - 1, retries, timeoutMs, budgetMs });
}
export function writeProbe() {
  if (Date.now() >= expiresAt - 10000) exec.test.abort('Session pool expired; preserve results and reconcile interrupted requests');
  const request = requestForIteration();
  const begin = Date.now();
  firstFailures.add(0); finalFailures.add(0); retryCount.add(0);
  emit({ event: 'started', ...request, generatorId, startedAt: begin });
  started.add(1);
  let response, success = false, attempts = 0, firstStatus = 0;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    const remaining = budgetMs - (Date.now() - begin);
    if (remaining <= 0) break;
    if (attempt > 0) retryCount.add(1);
    response = send(request, Math.max(1, Math.min(timeoutMs, remaining)));
    attempts += 1;
    if (attempt === 0) firstStatus = response.status;
    success = confirmedResponse(response.status, body(response), request);
    if (!success && attempt === 0) firstFailures.add(1);
    if (success || !retryableStatus(response.status) || attempt === retries) break;
    const wait = Math.min(100 * 2 ** attempt, Math.max(0, budgetMs - (Date.now() - begin)));
    sleep(wait / 1000);
  }
  const elapsed = Date.now() - begin;
  committed.add(success);
  endToEnd.add(elapsed);
  if (success) confirmed.add(1); else finalFailures.add(1);
  const status = response?.status || 0;
  emit({ event: 'final', ...request, generatorId, firstStatus, status, attempts, elapsedMs: elapsed,
    outcome: success ? 'confirmed' : [200, 201].includes(status) ? 'invalid_response' : retryableStatus(status) ? 'unconfirmed' : 'rejected' });
  if ([400, 401, 403, 404, 409, 413].includes(status)) exec.test.abort(`Probe contract/auth failed with ${status}; reconcile recorded requests`);
}
export function contract() {
  const request = requestForIteration();
  emit({ event: 'started', ...request, generatorId, startedAt: Date.now() });
  started.add(1);
  const first = send(request);
  const firstOK = first.status === 201 && confirmedResponse(first.status, body(first), request);
  emit({ event: 'final', ...request, generatorId, status: first.status, attempts: 1, outcome: firstOK ? 'confirmed' : 'unconfirmed' });
  if (firstOK) confirmed.add(1); else finalFailures.add(1);
  const repeated = send(request);
  const repeatedOK = repeated.status === 200 && confirmedResponse(repeated.status, body(repeated), request);
  const changed = { ...request, payloadHash: crypto.sha256(`conflict:${request.payloadHash}`, 'hex') };
  const conflict = send(changed, timeoutMs, http.expectedStatuses(409));
  const passed = firstOK && repeatedOK && conflict.status === 409;
  contractPass.add(passed);
  emit({ event: 'contract', runId, requestId: request.requestId, first: first.status, repeated: repeated.status, conflict: conflict.status, passed });
}
export function handleSummary(data) { return summaryOutput(data); }
