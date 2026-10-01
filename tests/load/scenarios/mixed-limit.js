import { check, fail, sleep } from 'k6';
import exec from 'k6/execution';
import http from 'k6/http';
import { Counter, Rate } from 'k6/metrics';

import { createTrip, issueCsrfToken, uploadBatch, viewTrip } from '../lib/api.mjs';
import { csvEnv, normalizeBaseUrl, optionalEnv, positiveIntegerEnv, requiredEnv, safeTripName, utcDateOffset } from '../lib/config.mjs';
import { loadDataset, splitBatches } from '../lib/dataset.mjs';
import { buildMultipart } from '../lib/multipart.mjs';
import { businessFailures, photosUploaded, successfulJourneys, uploadFinalDuration } from '../lib/metrics.mjs';
import { accountIndexForScenario, buildMixedScenarios } from '../lib/mixed-accounts.mjs';
import { initializeRefreshableSession, refreshIfDue } from '../lib/refresh-session.mjs';
import { emitRunEvent, summaryOutput } from '../lib/summary.mjs';

const baseUrl = normalizeBaseUrl(requiredEnv('K6_BASE_URL'));
const creationTokens = csvEnv('K6_CREATION_REFRESH_TOKENS', '');
const viewTokens = csvEnv('K6_VIEW_REFRESH_TOKENS', '');
const viewTripIds = csvEnv('K6_VIEW_TRIP_IDS', '').map(Number);
const multiplier = positiveIntegerEnv('K6_LOAD_MULTIPLIER', 1);
const duration = optionalEnv('K6_LIMIT_DURATION', '1h');
const viewThinkTimeSeconds = positiveIntegerEnv('K6_GENERAL_VIEW_THINK_TIME_SECONDS', 2);
const dataset = loadDataset(requiredEnv('K6_DATA_MANIFEST'));
const batches = splitBatches(dataset.files, 10);
const viewSuccess = new Rate('yeodam_general_view_success');
const processingPolls = new Counter('yeodam_processing_polls');

if (viewTokens.length !== viewTripIds.length || viewTripIds.some((id) => !Number.isInteger(id) || id < 1)) {
  throw new Error('K6_VIEW_REFRESH_TOKENS와 K6_VIEW_TRIP_IDS에는 같은 개수의 계정·완료 여행 ID가 필요합니다.');
}
if (new Set(creationTokens).size !== creationTokens.length) {
  throw new Error('K6_CREATION_REFRESH_TOKENS에 중복된 Token이 있습니다. 생성 VU마다 서로 다른 계정을 사용하세요.');
}
if (new Set([...creationTokens, ...viewTokens]).size !== creationTokens.length + viewTokens.length) {
  throw new Error('생성·조회 VU 사이에 중복된 Refresh Token이 있습니다. 계정별 독립 세션을 사용하세요.');
}
if (creationTokens.length < multiplier + 1) {
  throw new Error('생성 계정은 부하 배수보다 최소 1개 더 필요합니다. 처리시간 변동에 대비한 유입 여유를 확보하세요.');
}

export const options = {
  discardResponseBodies: true,
  noCookiesReset: true,
  scenarios: buildMixedScenarios(creationTokens.length, viewTokens.length, multiplier, duration),
  thresholds: {
    checks: ['rate==1'],
    http_req_failed: ['rate<0.01'],
    dropped_iterations: ['count==0'],
    yeodam_business_failures: ['count==0'],
    yeodam_successful_journeys: ['rate==1'],
    yeodam_general_view_success: ['rate==1'],
    'yeodam_trip_view_duration{workload:general_view}': ['p(95)<1000'],
  },
};

function requestId(response) {
  for (const [name, value] of Object.entries(response.headers || {})) {
    if (name.toLowerCase() === 'x-request-id') return value;
  }
  return null;
}

function requireOk(response, operation) {
  const ok = check(response, { [`${operation}: status 200`]: (result) => result.status === 200 });
  if (!ok) {
    businessFailures.add(1, { operation });
    fail(`${operation}: 실제 status=${response.status}, request_id=${requestId(response)}`);
  }
}

function get(baseUrlValue, path, operation) {
  const response = http.get(`${baseUrlValue}/api${path}`, {
    responseType: 'none',
    tags: { operation, endpoint: path.replace(/\/[0-9]+/gu, '/:id') },
    timeout: '30s',
  });
  requireOk(response, operation);
  return requestId(response);
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function uploadFinalWithPolling(refreshToken, csrfToken, tripId, files, batchNo, totalCount, ids) {
  const parts = files.map((file) => ({
    name: 'attachments[]',
    data: file.data,
    filename: file.filename,
    contentType: file.contentType,
  }));
  parts.push(
    { name: 'batchNo', data: String(batchNo) },
    { name: 'totalAttachmentCount', data: String(totalCount) },
    { name: 'complete', data: 'true' },
  );
  const multipart = buildMultipart(parts);
  let settled = false;
  let pollCount = 0;
  const responsePromise = http.asyncRequest(
    'POST',
    `${baseUrl}/api/trips/${tripId}/initial-attachments`,
    multipart.body,
    {
      headers: { 'Content-Type': multipart.contentType, 'X-CSRF-TOKEN': csrfToken },
      responseType: 'text',
      tags: { endpoint: 'trip_initial_attachments', operation: 'upload_final', batch_result: 'final' },
      timeout: '15m',
    },
  ).then((response) => {
    settled = true;
    return response;
  }, (error) => {
    settled = true;
    throw error;
  });

  while (!settled) {
    await delay(2000);
    if (!settled) {
      refreshIfDue(baseUrl, refreshToken);
      processingPolls.add(1);
      pollCount += 1;
      const id = get(baseUrl, `/trips/${tripId}/processing-status`, 'processing_status');
      if (id) ids.push(id);
    }
  }

  const response = await responsePromise;
  uploadFinalDuration.add(response.timings.duration);
  requireOk(response, 'upload_final');
  photosUploaded.add(files.length);
  ids.push(requestId(response));
  let payload;
  try {
    payload = response.json();
  } catch (_error) {
    businessFailures.add(1, { operation: 'upload_final' });
    fail(`upload_final: JSON 응답을 해석하지 못했습니다. trip_id=${tripId}`);
  }
  return { status: payload && payload.data, pollCount };
}

export async function createJourney() {
  const startedAt = new Date().toISOString();
  const ids = [];
  let tripId = null;
  businessFailures.add(0, { operation: 'trip_creation' });
  try {
    const token = creationTokens[accountIndexForScenario(exec.scenario.name, 'trip_creation', creationTokens.length)];
    ids.push(initializeRefreshableSession(baseUrl, token));
    const created = createTrip(baseUrl, issueCsrfToken(baseUrl), {
      tripName: safeTripName(optionalEnv('K6_TRIP_NAME_PREFIX', '한계')),
      startDate: optionalEnv('K6_TRIP_START_DATE', utcDateOffset(-1)),
      endDate: optionalEnv('K6_TRIP_END_DATE', utcDateOffset(0)),
      regionCodes: csvEnv('K6_REGION_CODES', '50110'),
    });
    tripId = created.tripId;
    ids.push(created.requestId);
    const csrf = issueCsrfToken(baseUrl);
    for (let index = 0; index < batches.length - 1; index += 1) {
      refreshIfDue(baseUrl, token);
      ids.push(uploadBatch(baseUrl, csrf, tripId, batches[index], index + 1, dataset.files.length, false).requestId);
    }
    const final = await uploadFinalWithPolling(
      token, csrf, tripId, batches[batches.length - 1], batches.length, dataset.files.length, ids,
    );
    const finalStatus = final.status;
    const resultCount = finalStatus && finalStatus.result
      ? finalStatus.result.classifiedAttachmentCount + finalStatus.result.unclassifiedAttachmentCount
      : null;
    const finalOk = check(finalStatus, {
      'trip_creation: completed': (status) => status && status.status === 'COMPLETED',
      'trip_creation: photo count': () => resultCount === dataset.files.length,
    });
    if (!finalOk) {
      businessFailures.add(1, { operation: 'trip_creation' });
      fail(`trip_creation: 최종 결과가 일치하지 않습니다. trip_id=${tripId}`);
    }
    const viewed = viewTrip(baseUrl, tripId);
    ids.push(viewed.requestId);
    const viewOk = check(viewed.detail, {
      'trip_creation: result view': (detail) => detail && detail.tripId === tripId,
    });
    if (!viewOk) {
      businessFailures.add(1, { operation: 'trip_creation_result_view' });
      fail(`trip_creation: 결과 조회가 일치하지 않습니다. trip_id=${tripId}`);
    }
    successfulJourneys.add(true);
    emitRunEvent({ event: 'journey_completed', scenario: 'trip_creation', startedAt, endedAt: new Date().toISOString(), tripId, photoCount: dataset.files.length, pollCount: final.pollCount, requestIds: ids.filter(Boolean), result: 'success' });
  } catch (error) {
    successfulJourneys.add(false);
    emitRunEvent({ event: 'journey_completed', scenario: 'trip_creation', startedAt, endedAt: new Date().toISOString(), tripId, requestIds: ids.filter(Boolean), result: 'failure', error: String(error) });
    if (/token_refresh|auth_check|account_assignment|status=401/u.test(String(error))) {
      exec.test.abort(`trip_creation: 인증 또는 계정 할당 실패로 중단했습니다. ${error}`);
    }
    throw error;
  }
}

export function viewJourney() {
  let tripId = null;
  const startedAt = new Date().toISOString();
  const ids = [];
  businessFailures.add(0, { operation: 'general_view' });
  try {
    const index = accountIndexForScenario(exec.scenario.name, 'general_view', viewTokens.length);
    const token = viewTokens[index];
    tripId = viewTripIds[index];
    ids.push(initializeRefreshableSession(baseUrl, token));
    sleep(viewThinkTimeSeconds);
    ids.push(get(baseUrl, '/trips', 'trip_list'));
    sleep(viewThinkTimeSeconds);
    ids.push(get(baseUrl, '/trips/map', 'trip_map'));
    sleep(viewThinkTimeSeconds);
    const viewed = viewTrip(baseUrl, tripId);
    ids.push(viewed.requestId);
    const valid = check(viewed.detail, {
      'general_view: requested trip': (detail) => detail && detail.tripId === tripId,
    });
    if (!valid) {
      businessFailures.add(1, { operation: 'general_view' });
      fail(`general_view: 여행 상세가 일치하지 않습니다. trip_id=${tripId}`);
    }
    sleep(viewThinkTimeSeconds);
    ids.push(get(baseUrl, `/trips/${tripId}/place-folders`, 'place_folders'));
    viewSuccess.add(true);
    emitRunEvent({ event: 'journey_completed', scenario: 'general_view', startedAt, endedAt: new Date().toISOString(), tripId, requestIds: ids.filter(Boolean), result: 'success' });
  } catch (error) {
    viewSuccess.add(false);
    emitRunEvent({ event: 'journey_completed', scenario: 'general_view', startedAt, endedAt: new Date().toISOString(), tripId, requestIds: ids.filter(Boolean), result: 'failure', error: String(error) });
    if (/token_refresh|auth_check|account_assignment|status=401/u.test(String(error))) {
      exec.test.abort(`general_view: 인증 또는 계정 할당 실패로 중단했습니다. ${error}`);
    }
    throw error;
  }
}

export function setup() {
  emitRunEvent({
    event: 'run_started', scenario: 'mixed_limit', startedAt: new Date().toISOString(),
    multiplier, duration, creationSessionsPerHour: 4 * multiplier,
    viewSessionsPerHour: 11 * multiplier, viewThinkTimeSeconds,
    creationAccounts: creationTokens.length,
    viewAccounts: viewTokens.length, viewTripIds,
    dataset: dataset.name, datasetVersion: dataset.version,
    photoCount: dataset.files.length, totalBytes: dataset.totalBytes,
    cloudRelease: optionalEnv('K6_CLOUD_RELEASE', 'unknown'),
    frontendRelease: optionalEnv('K6_FRONTEND_RELEASE', 'unknown'),
    backendRelease: optionalEnv('K6_BACKEND_RELEASE', 'unknown'),
    aiRelease: optionalEnv('K6_AI_RELEASE', 'unknown'),
  });
}

export function teardown() {
  emitRunEvent({ event: 'run_completed', scenario: 'mixed_limit', endedAt: new Date().toISOString() });
}

export function handleSummary(data) {
  return summaryOutput(data);
}
