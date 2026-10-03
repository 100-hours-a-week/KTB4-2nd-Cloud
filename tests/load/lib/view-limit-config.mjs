const MAX_SESSIONS_PER_MINUTE = 600;
const MAX_STAGE_SECONDS = 300;

export function parseTripIds(value) {
  const ids = String(value || '').split(',').map((part) => Number(part.trim()));
  if (ids.length === 0 || ids.some((id) => !Number.isSafeInteger(id) || id < 1)) {
    throw new Error('K6_VIEW_TRIP_IDS는 소유한 완료 여행 ID의 쉼표 목록이어야 합니다.');
  }
  return ids;
}

export function stageSeconds(value) {
  const match = /^(\d+)(s|m)$/u.exec(value);
  const seconds = match ? Number(match[1]) * (match[2] === 'm' ? 60 : 1) : NaN;
  if (!Number.isSafeInteger(seconds) || seconds < 10 || seconds > MAX_STAGE_SECONDS) {
    throw new Error('K6_VIEW_LIMIT_DURATION은 10s~5m 범위여야 합니다.');
  }
  return seconds;
}

export function buildViewLimitScenario(sessionsPerMinute, duration, thinkTimeSeconds) {
  if (!Number.isSafeInteger(sessionsPerMinute) || sessionsPerMinute < 1
      || sessionsPerMinute > MAX_SESSIONS_PER_MINUTE) {
    throw new Error('K6_VIEW_LIMIT_SESSIONS_PER_MINUTE은 1~600 정수여야 합니다.');
  }
  if (!Number.isSafeInteger(thinkTimeSeconds) || thinkTimeSeconds < 1 || thinkTimeSeconds > 5) {
    throw new Error('K6_VIEW_THINK_TIME_SECONDS는 1~5 정수여야 합니다.');
  }
  stageSeconds(duration);

  // 네 요청 사이의 think time과 요청당 1초를 반영하고 여유 VU를 둔다.
  const expectedJourneySeconds = 4 * (thinkTimeSeconds + 1);
  const preAllocatedVUs = Math.ceil(sessionsPerMinute * expectedJourneySeconds / 60) + 2;
  return {
    executor: 'constant-arrival-rate',
    rate: sessionsPerMinute,
    timeUnit: '1m',
    duration,
    preAllocatedVUs,
    maxVUs: preAllocatedVUs * 2,
    gracefulStop: '30s',
    exec: 'viewLimit',
    tags: { test_scope: 'view_limit', workload: 'general_view' },
  };
}
