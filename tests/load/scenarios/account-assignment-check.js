import { check } from 'k6';
import exec from 'k6/execution';

import { accountIndexForScenario, buildMixedScenarios, minimumCreationAccounts } from '../lib/mixed-accounts.mjs';

const multiplier = Number(__ENV.K6_LOAD_MULTIPLIER || 1);
const creationCount = Number(__ENV.K6_ACCOUNT_CHECK_CREATION_COUNT || 2);
const viewCount = Number(__ENV.K6_ACCOUNT_CHECK_VIEW_COUNT || 1);
if (creationCount < minimumCreationAccounts(multiplier)) {
  throw new Error(`${multiplier}배수의 생성 계정은 최소 ${minimumCreationAccounts(multiplier)}개가 필요합니다.`);
}
const planned = buildMixedScenarios(creationCount, viewCount, multiplier, '1h');

export const options = {
  scenarios: Object.fromEntries(Object.entries(planned).map(([name, config]) => [name, {
    executor: 'per-vu-iterations', vus: 1, iterations: 1, maxDuration: '10s',
    exec: 'verifyAssignment', tags: config.tags,
  }])),
  thresholds: { checks: ['rate==1'] },
};

export function verifyAssignment() {
  const name = exec.scenario.name;
  const workload = name.startsWith('trip_creation_') ? 'trip_creation' : 'general_view';
  const count = workload === 'trip_creation' ? creationCount : viewCount;
  const index = accountIndexForScenario(name, workload, count);
  check(index, { '계정 Slot이 유효함': (value) => value >= 0 && value < count });
  console.log(JSON.stringify({ scenario: name, accountSlot: index + 1, vuIdInTest: exec.vu.idInTest }));
}
