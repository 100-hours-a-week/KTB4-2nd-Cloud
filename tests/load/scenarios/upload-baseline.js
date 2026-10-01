import { check, fail } from 'k6';

import {
  createTrip,
  initializeAuthenticatedSession,
  issueCsrfToken,
  uploadBatch,
  viewTrip,
} from '../lib/api.mjs';
import {
  csvEnv,
  normalizeBaseUrl,
  optionalEnv,
  requiredEnv,
  safeTripName,
  utcDateOffset,
} from '../lib/config.mjs';
import { loadDataset, splitBatches } from '../lib/dataset.mjs';
import { businessFailures, successfulJourneys } from '../lib/metrics.mjs';
import { emitRunEvent, summaryOutput } from '../lib/summary.mjs';

const baseUrl = normalizeBaseUrl(requiredEnv('K6_BASE_URL'));
const accessToken = requiredEnv('K6_ACCESS_TOKEN');
const manifestPath = requiredEnv('K6_DATA_MANIFEST');
const dataset = loadDataset(manifestPath);
const batches = splitBatches(dataset.files, 10);

export const options = {
  discardResponseBodies: true,
  scenarios: {
    upload_baseline: {
      executor: 'shared-iterations',
      vus: 1,
      iterations: 1,
      maxDuration: '30m',
      gracefulStop: '20m',
      exec: 'uploadBaseline',
      tags: { test_scope: 'baseline', workload: 'trip_creation' },
    },
  },
  thresholds: {
    checks: ['rate==1'],
    http_req_failed: ['rate<0.01'],
    yeodam_business_failures: ['count==0'],
    yeodam_successful_journeys: ['rate==1'],
  },
};

export function uploadBaseline() {
  businessFailures.add(0, { operation: 'journey' });
  const startedAt = new Date().toISOString();
  const tripName = safeTripName(optionalEnv('K6_TRIP_NAME_PREFIX', '부하'));
  const startDate = optionalEnv('K6_TRIP_START_DATE', utcDateOffset(-1));
  const endDate = optionalEnv('K6_TRIP_END_DATE', utcDateOffset(0));
  const regionCodes = csvEnv('K6_REGION_CODES', '50110');
  const requestIds = [];
  let tripId = null;

  emitRunEvent({
    event: 'run_started',
    scenario: 'upload_baseline',
    startedAt,
    dataset: dataset.name,
    datasetVersion: dataset.version,
    photoCount: dataset.files.length,
    totalBytes: dataset.totalBytes,
    cloudRelease: optionalEnv('K6_CLOUD_RELEASE', 'unknown'),
    frontendRelease: optionalEnv('K6_FRONTEND_RELEASE', 'unknown'),
    backendRelease: optionalEnv('K6_BACKEND_RELEASE', 'unknown'),
    aiRelease: optionalEnv('K6_AI_RELEASE', 'unknown'),
  });

  try {
    requestIds.push(initializeAuthenticatedSession(baseUrl, accessToken));
    const createCsrfToken = issueCsrfToken(baseUrl);
    const created = createTrip(baseUrl, createCsrfToken, {
      tripName,
      startDate,
      endDate,
      regionCodes,
    });
    tripId = created.tripId;
    requestIds.push(created.requestId);

    const uploadCsrfToken = issueCsrfToken(baseUrl);
    let finalStatus = null;
    for (let index = 0; index < batches.length; index += 1) {
      const complete = index === batches.length - 1;
      const uploaded = uploadBatch(
        baseUrl,
        uploadCsrfToken,
        tripId,
        batches[index],
        index + 1,
        dataset.files.length,
        complete,
      );
      requestIds.push(uploaded.requestId);
      if (complete) finalStatus = uploaded.status;
    }

    const resultCount =
      finalStatus && finalStatus.result
        ? finalStatus.result.classifiedAttachmentCount + finalStatus.result.unclassifiedAttachmentCount
        : null;
    const finalOk = check(finalStatus, {
      'upload_final: status COMPLETED': (status) => status && status.status === 'COMPLETED',
      'upload_final: result photo count matches': () => resultCount === dataset.files.length,
    });
    if (!finalOk) {
      businessFailures.add(1, { operation: 'upload_final_result' });
      fail(`최종 처리 결과가 데이터셋과 일치하지 않습니다. trip_id=${tripId}`);
    }

    const viewed = viewTrip(baseUrl, tripId);
    requestIds.push(viewed.requestId);
    const viewOk = check(viewed.detail, {
      'trip_view: trip id matches': (detail) => detail && detail.tripId === tripId,
    });
    if (!viewOk) {
      businessFailures.add(1, { operation: 'trip_view_result' });
      fail(`여행 상세 결과가 데이터셋과 일치하지 않습니다. trip_id=${tripId}`);
    }

    successfulJourneys.add(true);
    emitRunEvent({
      event: 'run_completed',
      scenario: 'upload_baseline',
      startedAt,
      endedAt: new Date().toISOString(),
      tripId,
      requestIds: requestIds.filter(Boolean),
      photoCount: dataset.files.length,
      totalBytes: dataset.totalBytes,
      result: 'success',
    });
  } catch (error) {
    successfulJourneys.add(false);
    emitRunEvent({
      event: 'run_completed',
      scenario: 'upload_baseline',
      startedAt,
      endedAt: new Date().toISOString(),
      tripId,
      requestIds: requestIds.filter(Boolean),
      result: 'failure',
      error: String(error),
    });
    throw error;
  }
}

export function handleSummary(data) {
  return summaryOutput(data);
}
