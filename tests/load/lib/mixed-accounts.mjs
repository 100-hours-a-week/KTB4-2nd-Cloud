const HOUR_MS = 60 * 60 * 1000;

function durationMilliseconds(value) {
  if (typeof value !== 'string' || !/^(?:\d+(?:ms|s|m|h))+$/u.test(value)) {
    throw new Error(`K6_LIMIT_DURATION은 1h, 30m, 1h30m 같은 k6 시간 형식이어야 합니다: ${value}`);
  }
  const units = { ms: 1, s: 1000, m: 60 * 1000, h: HOUR_MS };
  let total = 0;
  const parts = /(\d+)(ms|s|m|h)/gu;
  let match;
  while ((match = parts.exec(value)) !== null) total += Number(match[1]) * units[match[2]];
  if (!Number.isSafeInteger(total) || total < 1000) {
    throw new Error('K6_LIMIT_DURATION은 1초 이상이어야 합니다.');
  }
  return total;
}

export function minimumCreationAccounts(multiplier) {
  return 2 * multiplier;
}

function addWorkloadScenarios(scenarios, workload, accountCount, sessionsPerHour, durationMs) {
  if (!Number.isInteger(accountCount) || accountCount < 1) {
    throw new Error(`${workload}: 계정이 최소 1개 필요합니다.`);
  }
  const totalStarts = Math.ceil((durationMs * sessionsPerHour) / HOUR_MS - 1e-9);
  const activeCount = Math.min(totalStarts, accountCount);
  const globalIntervalMs = HOUR_MS / sessionsPerHour;
  const accountIntervalMs = globalIntervalMs * activeCount;

  for (let index = 0; index < activeCount; index += 1) {
    const starts = Math.ceil((totalStarts - index) / activeCount);
    const startTimeMs = Math.round(index * globalIntervalMs);
    const intervalMs = Math.round(accountIntervalMs);
    // 마지막 목표 시작 직후 종료하여 1시간 경계의 추가 시작을 막는다.
    const scenarioDurationMs = Math.round((starts - 1) * accountIntervalMs) + Math.min(1000, Math.floor(intervalMs / 2));
    scenarios[`${workload}_${index + 1}`] = {
      executor: 'constant-arrival-rate', rate: 1, timeUnit: `${intervalMs}ms`,
      startTime: `${startTimeMs}ms`, duration: `${scenarioDurationMs}ms`,
      preAllocatedVUs: 1, maxVUs: 1,
      gracefulStop: workload === 'trip_creation' ? '35m' : '2m',
      exec: workload === 'trip_creation' ? 'createJourney' : 'viewJourney',
      tags: { test_scope: 'mixed_limit', workload },
    };
  }
}

export function buildMixedScenarios(creationAccountCount, viewAccountCount, multiplier, duration) {
  if (!Number.isInteger(multiplier) || multiplier < 1) {
    throw new Error('K6_LOAD_MULTIPLIER는 양의 정수여야 합니다.');
  }
  const durationMs = durationMilliseconds(duration);
  const scenarios = {};
  addWorkloadScenarios(scenarios, 'trip_creation', creationAccountCount, 4 * multiplier, durationMs);
  addWorkloadScenarios(scenarios, 'general_view', viewAccountCount, 11 * multiplier, durationMs);
  return scenarios;
}

export function accountIndexForScenario(name, workload, accountCount) {
  const prefix = `${workload}_`;
  const slot = name.startsWith(prefix) ? Number(name.slice(prefix.length)) : NaN;
  if (!Number.isInteger(slot) || slot < 1 || slot > accountCount) {
    throw new Error(`account_assignment: ${name}에 연결된 ${workload} 계정이 없습니다.`);
  }
  return slot - 1;
}
