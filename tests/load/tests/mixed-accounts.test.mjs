import assert from 'node:assert/strict';
import test from 'node:test';

import { accountIndexForScenario, buildMixedScenarios, minimumCreationAccounts } from '../lib/mixed-accounts.mjs';

function plannedStarts(scenarios, workload) {
  return Object.entries(scenarios)
    .filter(([, value]) => value.tags.workload === workload)
    .flatMap(([name, value]) => {
      const start = Number.parseInt(value.startTime, 10);
      const interval = Number.parseInt(value.timeUnit, 10) / value.rate;
      const duration = Number.parseInt(value.duration, 10);
      return Array.from({ length: Math.ceil(duration / interval) }, (_, index) => ({ name, at: start + index * interval }));
    })
    .sort((left, right) => left.at - right.at);
}

for (const [multiplier, creationCount, viewCount] of [[1, 2, 1], [2, 4, 2], [3, 6, 2]]) {
  test(`${multiplier}배수에서 목표 건수, 시작 간격, 계정 할당을 유지한다`, () => {
    const scenarios = buildMixedScenarios(creationCount, viewCount, multiplier, '1h');
    const creation = Object.entries(scenarios).filter(([, value]) => value.tags.workload === 'trip_creation');
    const view = Object.entries(scenarios).filter(([, value]) => value.tags.workload === 'general_view');
    const creationStarts = plannedStarts(scenarios, 'trip_creation');
    const viewStarts = plannedStarts(scenarios, 'general_view');

    assert.equal(creationStarts.length, 4 * multiplier);
    assert.equal(viewStarts.length, 11 * multiplier);
    assert.equal(creationStarts[0].at, 0);
    assert.equal(viewStarts[0].at, 0);
    assert.ok(creationStarts.at(-1).at < 3_600_000);
    assert.ok(viewStarts.at(-1).at < 3_600_000);
    for (const [starts, total] of [[creationStarts, 4 * multiplier], [viewStarts, 11 * multiplier]]) {
      for (let index = 1; index < starts.length; index += 1) {
        assert.ok(Math.abs((starts[index].at - starts[index - 1].at) - 3_600_000 / total) < 2);
      }
    }
    assert.equal(creation.length, creationCount);
    assert.equal(view.length, viewCount);
    assert.equal(minimumCreationAccounts(multiplier), 2 * multiplier);

    for (const [index, [name, value]] of creation.entries()) {
      assert.equal(accountIndexForScenario(name, 'trip_creation', creationCount), index);
      assert.equal(value.maxVUs, 1);
      assert.equal(creationStarts.filter((start) => start.name === name).length, 2);
      assert.equal(Number.parseInt(value.timeUnit, 10), 1_800_000);
    }
    for (const [index, [name, value]] of view.entries()) {
      assert.equal(accountIndexForScenario(name, 'general_view', viewCount), index);
      assert.equal(value.maxVUs, 1);
    }
  });
}

test('계정별 세션 수가 다르더라도 종료 경계에서 추가 시작하지 않는다', () => {
  const scenarios = buildMixedScenarios(3, 2, 2, '1h');
  assert.equal(plannedStarts(scenarios, 'trip_creation').length, 8);
  assert.equal(plannedStarts(scenarios, 'general_view').length, 22);
  assert.equal(plannedStarts(scenarios, 'trip_creation').filter((start) => start.name === 'trip_creation_3').length, 2);
});

test('짧은 실행시간에도 계획한 경계 안에서만 시작한다', () => {
  const scenarios = buildMixedScenarios(2, 1, 1, '30m');
  assert.equal(plannedStarts(scenarios, 'trip_creation').length, 2);
  assert.equal(plannedStarts(scenarios, 'general_view').length, 6);
  assert.ok(plannedStarts(scenarios, 'general_view').at(-1).at < 1_800_000);
});

test('잘못된 실행시간을 거부한다', () => {
  assert.throws(() => buildMixedScenarios(2, 1, 1, '0s'), /K6_LIMIT_DURATION/u);
  assert.throws(() => buildMixedScenarios(2, 1, 1, 'one hour'), /K6_LIMIT_DURATION/u);
});

test('잘못된 Scenario 이름과 계정 범위를 거부한다', () => {
  assert.throws(() => accountIndexForScenario('trip_creation_3', 'trip_creation', 2), /account_assignment/u);
  assert.throws(() => accountIndexForScenario('general_view_1', 'trip_creation', 2), /account_assignment/u);
});
