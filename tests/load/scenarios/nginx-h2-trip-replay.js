import http from 'k6/http';
import crypto from 'k6/crypto';
import exec from 'k6/execution';
import { sleep } from 'k6';
import { SharedArray } from 'k6/data';
import { Counter, Rate, Trend } from 'k6/metrics';
import { config, origin, slots } from '../lib/nginx-h2-replay.mjs';

const profile = new SharedArray('h2-442-profile', () => [JSON.parse(open(__ENV.K6_H2_PROFILE))])[0];
const c = config(__ENV, profile);
const pool = new SharedArray('h2-442-private-sessions', () => [JSON.parse(open(__ENV.K6_H2_POOL))])[0];
if (pool.baseUrl !== origin || pool.version !== 1 || pool.sessions.length !== 3 ||
    Date.parse(pool.expiresAt) <= Date.now() + (c.seconds + c.warmupSeconds + 90) * 1000) {
  throw new Error('Fresh three-owner synthetic pool required');
}
const started = new Counter('h2_post_started');
const committed = new Counter('h2_post_committed');
const rejected = new Counter('h2_post_rejected');
const failed = new Counter('h2_post_unconfirmed');
const missed = new Counter('h2_schedule_missed');
const expected = new Rate('h2_expected_response');
const duration = new Trend('h2_post_elapsed_ms', true);
const scheduling = new Trend('h2_schedule_lag_ms', true);
const session = pool.sessions[0]; // One own short-lived session across clients; no refresh during load.
const prefix = `h${crypto.sha256(c.runId, 'hex').slice(0, 4)}`;

export const options = {
  noConnectionReuse: false, noVUConnectionReuse: false, maxRedirects: 0,
  discardResponseBodies: true,
  tags: { testid: c.runId, kind: 'nginx_http2_replay_442' },
  scenarios: { replay: { executor: 'per-vu-iterations', vus: c.vus, iterations: 1,
    maxDuration: `${c.warmupSeconds + c.seconds + 10}s`, gracefulStop: '5s' } },
  thresholds: { h2_expected_response: ['rate==1'], h2_schedule_missed: ['count==0'] },
};

function event(data) { console.log(JSON.stringify({ source: 'nginx-h2-442', runId: c.runId, ...data })); }
function waitUntil(time) { const left = (time - Date.now()) / 1000; if (left > 0) sleep(left); }
function post(sequence, phase, due) {
  if (Date.now() >= Date.parse(pool.expiresAt) - 30000) exec.test.abort('Session TTL guard');
  const tripName = prefix + sequence.toString(36).padStart(5, '0');
  if (tripName.length !== 10) exec.test.abort('Trip name namespace exhausted');
  const at = Date.now();
  const lag = Math.max(0, at - due); scheduling.add(lag);
  started.add(1, { phase });
  event({ event: 'started', phase, vu: __VU, sequence, tripName, userId: session.userId, atMs: at });
  const response = http.post(origin + '/api/trips', JSON.stringify({ tripName,
    startDate: '2025-01-01', endDate: '2025-01-03', regionCodes: ['50110'] }), {
    cookies: { accessToken: { value: session.accessToken }, CSRF_CONTEXT: { value: session.csrfContext } },
    headers: { 'Content-Type': 'application/json', 'X-CSRF-TOKEN': session.csrfToken,
      'User-Agent': 'yeodam-h2-replay442' }, responseType: 'text', redirects: 0, timeout: '3s',
    responseCallback: http.expectedStatuses(201, 429), tags: { phase, name: 'POST /api/trips' },
  });
  let tripId = null;
  if (response.status === 201) { try { tripId = response.json()?.data?.tripId; } catch (_) { /* Audit ambiguous result. */ } }
  const ok = response.proto === 'HTTP/2.0' &&
    ((response.status === 201 && Number.isInteger(tripId)) || response.status === 429);
  expected.add(ok); duration.add(Date.now() - at);
  if (response.status === 201 && Number.isInteger(tripId)) committed.add(1);
  else if (response.status === 429) rejected.add(1);
  else failed.add(1);
  const rid = Object.entries(response.headers || {}).find(([k]) => k.toLowerCase() === 'x-request-id')?.[1] || null;
  event({ event: 'response', phase, vu: __VU, sequence, tripName, userId: session.userId,
    atMs: at, endedAtMs: Date.now(), lagMs: lag, status: response.status, protocol: response.proto,
    tripId, requestId: rid, errorCode: response.error_code || null,
    timingMs: { blocked: response.timings.blocked, connecting: response.timings.connecting,
      tls: response.timings.tls_handshaking, waiting: response.timings.waiting,
      duration: response.timings.duration } });
  // Never repeat an ambiguous POST. A failed warmup client exits; independent normal guards supervise the rest.
  return ok;
}

export function setup() {
  missed.add(0); failed.add(0);
  const start = Date.now() + 1500;
  event({ event: 'run_started', startMs: start, vus: c.vus, expectedReplayRequests: c.expected,
    seconds: c.seconds, warmupSeconds: c.warmupSeconds, rate: c.rate || 'recorded_profile',
    api: 'POST /api/trips', expectedProtocol: 'HTTP/2.0',
    maxInFlightPerClient: 1, noPhotoUploadOrAI: true,
    scope: 'Client VUs are not asserted TCP connection count; reconcile server connection IDs' });
  return { start };
}

export default function (data) {
  const client = __VU - 1;
  const warmupAt = data.start + client / c.warmupOpeningRate * 1000;
  waitUntil(warmupAt);
  if (!post(client, 'warmup', warmupAt)) return;
  const began = data.start + c.warmupSeconds * 1000;
  let offset = c.vus;
  for (let second = 0; second < c.seconds; second++) {
    const count = c.rate || c.shape[second];
    for (const slot of slots(count, c.vus, client, offset)) {
      const due = began + (second + slot / count) * 1000;
      waitUntil(due);
      if (Date.now() - due > 250) { missed.add(1); continue; }
      if (!post(offset + slot, 'replay', due)) return;
    }
    offset += count;
  }
}

export function teardown() { event({ event: 'run_finished', atMs: Date.now() }); }
