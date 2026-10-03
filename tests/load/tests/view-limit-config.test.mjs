import assert from 'node:assert/strict';
import test from 'node:test';

import { buildViewLimitScenario, parseTripIds, stageSeconds } from '../lib/view-limit-config.mjs';

test('조회 세션 시작률과 VU 여유를 고정한다', () => {
  const scenario = buildViewLimitScenario(48, '3m', 2);
  assert.equal(scenario.executor, 'constant-arrival-rate');
  assert.equal(scenario.rate, 48);
  assert.equal(scenario.timeUnit, '1m');
  assert.equal(scenario.duration, '3m');
  assert.equal(scenario.preAllocatedVUs, 12);
  assert.equal(scenario.maxVUs, 24);
  assert.equal(scenario.exec, 'viewLimit');
});

test('운영 시험의 요청률과 시간 상한을 검사한다', () => {
  assert.equal(stageSeconds('10s'), 10);
  assert.equal(stageSeconds('5m'), 300);
  assert.throws(() => buildViewLimitScenario(0, '3m', 2));
  assert.throws(() => buildViewLimitScenario(601, '3m', 2));
  assert.throws(() => buildViewLimitScenario(6, '6m', 2));
  assert.throws(() => buildViewLimitScenario(6, '3m', 0));
});

test('조회 여행 ID에는 유효한 정수만 허용한다', () => {
  assert.deepEqual(parseTripIds('160, 159,152'), [160, 159, 152]);
  for (const value of ['', '160,', '0', '12.5', 'NaN']) {
    assert.throws(() => parseTripIds(value));
  }
});
