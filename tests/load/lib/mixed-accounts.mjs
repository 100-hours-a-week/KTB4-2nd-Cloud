function ratesPerAccount(totalSessions, accountCount) {
  const activeCount = Math.min(totalSessions, accountCount);
  const baseRate = Math.floor(totalSessions / activeCount);
  const remainder = totalSessions % activeCount;
  return Array.from({ length: activeCount }, (_, index) => baseRate + (index < remainder ? 1 : 0));
}

export function buildMixedScenarios(creationAccountCount, viewAccountCount, multiplier, duration) {
  const scenarios = {};

  for (const [index, rate] of ratesPerAccount(4 * multiplier, creationAccountCount).entries()) {
    scenarios[`trip_creation_${index + 1}`] = {
      executor: 'constant-arrival-rate', rate, timeUnit: '1h', duration,
      preAllocatedVUs: 1, maxVUs: 1, gracefulStop: '35m', exec: 'createJourney',
      tags: { test_scope: 'mixed_limit', workload: 'trip_creation' },
    };
  }

  for (const [index, rate] of ratesPerAccount(11 * multiplier, viewAccountCount).entries()) {
    scenarios[`general_view_${index + 1}`] = {
      executor: 'constant-arrival-rate', rate, timeUnit: '1h', duration,
      preAllocatedVUs: 1, maxVUs: 1, gracefulStop: '2m', exec: 'viewJourney',
      tags: { test_scope: 'mixed_limit', workload: 'general_view' },
    };
  }

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
