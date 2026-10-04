import { check, sleep } from 'k6';
import exec from 'k6/execution';
import http from 'k6/http';
import { Counter, Rate, Trend } from 'k6/metrics';

import { initializeAuthenticatedSession } from '../lib/api.mjs';
import { normalizeBaseUrl, optionalEnv, positiveIntegerEnv, requiredEnv } from '../lib/config.mjs';
import { businessFailures } from '../lib/metrics.mjs';
import { emitRunEvent, summaryOutput } from '../lib/summary.mjs';
import { buildViewLimitScenario, parseTripIds } from '../lib/view-limit-config.mjs';

const baseUrl = normalizeBaseUrl(requiredEnv('K6_BASE_URL'));
const accessToken = requiredEnv('K6_ACCESS_TOKEN');
const tripIds = parseTripIds(requiredEnv('K6_VIEW_TRIP_IDS'));
const sessionsPerMinute = positiveIntegerEnv('K6_VIEW_LIMIT_SESSIONS_PER_MINUTE', 6);
const duration = optionalEnv('K6_VIEW_LIMIT_DURATION', '3m');
const thinkTimeSeconds = positiveIntegerEnv('K6_VIEW_THINK_TIME_SECONDS', 2);
const scenario = buildViewLimitScenario(sessionsPerMinute, duration, thinkTimeSeconds);

const successfulViews = new Rate('yeodam_view_limit_success');
const readRequests = new Counter('yeodam_view_limit_read_requests');
const tripDetailDuration = new Trend('yeodam_view_limit_trip_detail_duration', true);

export const options = {
  discardResponseBodies: true,
  noCookiesReset: true,
  scenarios: { view_limit: scenario },
  thresholds: {
    http_req_failed: [{ threshold: 'rate<0.01', abortOnFail: true, delayAbortEval: '30s' }],
    dropped_iterations: [{ threshold: 'count==0', abortOnFail: true, delayAbortEval: '30s' }],
    yeodam_view_limit_success: [{ threshold: 'rate>=0.99', abortOnFail: true, delayAbortEval: '30s' }],
    yeodam_view_limit_trip_detail_duration: [
      { threshold: 'p(95)<1000', abortOnFail: true, delayAbortEval: '30s' },
    ],
  },
};

let authenticated = false;
let failureLogged = false;

function requestId(response) {
  for (const [name, value] of Object.entries(response.headers || {})) {
    if (name.toLowerCase() === 'x-request-id') return value;
  }
  return null;
}

function recordFailure(operation, response, reason) {
  businessFailures.add(1, { operation });
  if (!failureLogged) {
    emitRunEvent({
      event: 'view_failure', operation, status: response.status,
      requestId: requestId(response), reason,
    });
    failureLogged = true;
  }
}

function read(path, operation, parseBody = false) {
  const response = http.get(`${baseUrl}/api${path}`, {
    responseType: parseBody ? 'text' : 'none',
    tags: { endpoint: path.replace(/\/[0-9]+/gu, '/:id'), operation },
    timeout: '30s',
  });
  readRequests.add(1, { operation });
  if (operation === 'trip_view') tripDetailDuration.add(response.timings.duration);
  if (response.status === 401) {
    exec.test.abort(`view_limit: 인증이 만료돼 중단했습니다. request_id=${requestId(response)}`);
  }
  const ok = check(response, { [`${operation}: status 200`]: (result) => result.status === 200 });
  if (!ok) recordFailure(operation, response, 'unexpected_status');
  return ok ? response : null;
}

export function viewLimit() {
  businessFailures.add(0, { operation: 'general_view' });
  if (!authenticated) {
    try {
      initializeAuthenticatedSession(baseUrl, accessToken);
      authenticated = true;
    } catch (error) {
      exec.test.abort(`view_limit: 사전 인증 확인 실패로 중단했습니다. ${error}`);
    }
  }

  const tripId = tripIds[exec.scenario.iterationInTest % tripIds.length];
  sleep(thinkTimeSeconds);
  if (!read('/trips', 'trip_list')) { successfulViews.add(false); return; }
  sleep(thinkTimeSeconds);
  if (!read('/trips/map', 'trip_map')) { successfulViews.add(false); return; }
  sleep(thinkTimeSeconds);
  const detail = read(`/trips/${tripId}`, 'trip_view', true);
  if (!detail) { successfulViews.add(false); return; }
  let valid = false;
  try {
    valid = detail.json().data?.tripId === tripId;
  } catch (_error) {
    // 잘못된 JSON도 완료 여행 조회 실패로 기록한다.
  }
  if (!check(valid, { 'trip_view: requested trip returned': (value) => value })) {
    recordFailure('trip_view', detail, 'trip_id_mismatch_or_invalid_json');
    successfulViews.add(false);
    return;
  }
  sleep(thinkTimeSeconds);
  if (!read(`/trips/${tripId}/place-folders`, 'place_folders')) { successfulViews.add(false); return; }
  successfulViews.add(true);
}

export function setup() {
  emitRunEvent({
    event: 'run_started', scenario: 'view_limit', startedAt: new Date().toISOString(),
    sessionsPerMinute, plannedApiRequestsPerSecond: sessionsPerMinute * 4 / 60,
    duration, thinkTimeSeconds, tripIds,
    cloudRelease: optionalEnv('K6_CLOUD_RELEASE', 'unknown'),
    backendRelease: optionalEnv('K6_BACKEND_RELEASE', 'unknown'),
  });
}

export function teardown() {
  emitRunEvent({ event: 'run_completed', scenario: 'view_limit', endedAt: new Date().toISOString() });
}

export function handleSummary(data) {
  return summaryOutput(data);
}
