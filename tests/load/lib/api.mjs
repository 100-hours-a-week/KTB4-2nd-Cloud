import { check, fail } from 'k6';
import http from 'k6/http';

import { buildMultipart } from './multipart.mjs';
import {
  businessFailures,
  photosUploaded,
  tripCreateDuration,
  tripViewDuration,
  uploadFinalDuration,
  uploadIntermediateDuration,
} from './metrics.mjs';

function responseRequestId(response) {
  for (const [name, value] of Object.entries(response.headers || {})) {
    if (name.toLowerCase() === 'x-request-id') return value;
  }
  return null;
}

function parseJson(response, operation) {
  try {
    return response.json();
  } catch (_error) {
    businessFailures.add(1, { operation });
    fail(`${operation}: JSON 응답을 해석하지 못했습니다. status=${response.status}`);
  }
}

function requireStatus(response, expected, operation) {
  const ok = check(response, {
    [`${operation}: status ${expected}`]: (result) => result.status === expected,
  });
  if (!ok) {
    businessFailures.add(1, { operation });
    fail(
      `${operation}: 예상 status=${expected}, 실제 status=${response.status}, request_id=${responseRequestId(response)}`,
    );
  }
}

export function initializeAuthenticatedSession(baseUrl, accessToken) {
  const jar = http.cookieJar();
  jar.set(baseUrl, 'accessToken', accessToken, {
    path: '/',
    secure: true,
  });

  const response = http.get(`${baseUrl}/api/users/me`, {
    responseType: 'none',
    tags: { endpoint: 'users_me', operation: 'auth_check' },
    timeout: '30s',
  });
  requireStatus(response, 200, 'auth_check');
  return responseRequestId(response);
}

export function issueCsrfToken(baseUrl) {
  const response = http.get(`${baseUrl}/api/auth/csrf`, {
    responseType: 'text',
    tags: { endpoint: 'auth_csrf', operation: 'csrf' },
    timeout: '30s',
  });
  requireStatus(response, 200, 'csrf');
  const payload = parseJson(response, 'csrf');
  const token = payload && payload.data && payload.data.token;
  if (!token) {
    businessFailures.add(1, { operation: 'csrf' });
    fail('csrf: 응답에 token이 없습니다.');
  }
  return token;
}

export function createTrip(baseUrl, csrfToken, request) {
  const response = http.post(`${baseUrl}/api/trips`, JSON.stringify(request), {
    headers: {
      'Content-Type': 'application/json',
      'X-CSRF-TOKEN': csrfToken,
    },
    responseType: 'text',
    tags: { endpoint: 'trips', operation: 'trip_create' },
    timeout: '30s',
  });
  tripCreateDuration.add(response.timings.duration);
  requireStatus(response, 201, 'trip_create');
  const payload = parseJson(response, 'trip_create');
  const tripId = payload && payload.data && payload.data.tripId;
  if (!Number.isInteger(tripId)) {
    businessFailures.add(1, { operation: 'trip_create' });
    fail('trip_create: 응답에 정수 tripId가 없습니다.');
  }
  return { tripId, requestId: responseRequestId(response) };
}

export function uploadBatch(baseUrl, csrfToken, tripId, files, batchNo, totalCount, complete) {
  const parts = files.map((file) => ({
    name: 'attachments[]',
    data: file.data,
    filename: file.filename,
    contentType: file.contentType,
  }));
  parts.push(
    { name: 'batchNo', data: String(batchNo) },
    { name: 'totalAttachmentCount', data: String(totalCount) },
    { name: 'complete', data: String(complete) },
  );
  const multipart = buildMultipart(parts);
  const response = http.post(`${baseUrl}/api/trips/${tripId}/initial-attachments`, multipart.body, {
    headers: {
      'Content-Type': multipart.contentType,
      'X-CSRF-TOKEN': csrfToken,
    },
    responseType: complete ? 'text' : 'none',
    tags: {
      endpoint: 'trip_initial_attachments',
      operation: complete ? 'upload_final' : 'upload_intermediate',
      batch_result: complete ? 'final' : 'intermediate',
    },
    timeout: complete ? '15m' : '10m',
  });

  if (complete) uploadFinalDuration.add(response.timings.duration);
  else uploadIntermediateDuration.add(response.timings.duration);

  requireStatus(response, complete ? 200 : 204, complete ? 'upload_final' : 'upload_intermediate');
  photosUploaded.add(files.length);

  let status = null;
  if (complete) {
    const payload = parseJson(response, 'upload_final');
    status = payload && payload.data;
  }
  return { status, requestId: responseRequestId(response), durationMs: response.timings.duration };
}

export function viewTrip(baseUrl, tripId) {
  const response = http.get(`${baseUrl}/api/trips/${tripId}`, {
    responseType: 'text',
    tags: { endpoint: 'trip_detail', operation: 'trip_view' },
    timeout: '30s',
  });
  tripViewDuration.add(response.timings.duration);
  requireStatus(response, 200, 'trip_view');
  const payload = parseJson(response, 'trip_view');
  return {
    detail: payload && payload.data,
    requestId: responseRequestId(response),
    durationMs: response.timings.duration,
  };
}

export function deleteTrip(baseUrl, csrfToken, tripId) {
  const response = http.del(`${baseUrl}/api/trips/${tripId}`, null, {
    headers: { 'X-CSRF-TOKEN': csrfToken },
    responseType: 'none',
    tags: { endpoint: 'trip_delete', operation: 'cleanup' },
    timeout: '30s',
  });
  requireStatus(response, 204, 'cleanup');
  return responseRequestId(response);
}
