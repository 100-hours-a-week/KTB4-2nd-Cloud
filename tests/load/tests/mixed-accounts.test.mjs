import assert from 'node:assert/strict';
import test from 'node:test';

import { accountIndexForScenario, buildMixedScenarios } from '../lib/mixed-accounts.mjs';

for (const [multiplier, creationCount, viewCount] of [[1, 2, 1], [2, 3, 2], [3, 4, 2]]) {
  test(`${multiplier}배수에서 세션률과 계정 할당을 유지한다`, () => {
    const scenarios = buildMixedScenarios(creationCount, viewCount, multiplier, '1h');
    const creation = Object.entries(scenarios).filter(([, value]) => value.tags.workload === 'trip_creation');
    const view = Object.entries(scenarios).filter(([, value]) => value.tags.workload === 'general_view');

    assert.equal(creation.reduce((sum, [, value]) => sum + value.rate, 0), 4 * multiplier);
    assert.equal(view.reduce((sum, [, value]) => sum + value.rate, 0), 11 * multiplier);
    assert.equal(creation.length, creationCount);
    assert.equal(view.length, viewCount);

    for (const [index, [name, value]] of creation.entries()) {
      assert.equal(accountIndexForScenario(name, 'trip_creation', creationCount), index);
      assert.equal(value.maxVUs, 1);
    }
    for (const [index, [name, value]] of view.entries()) {
      assert.equal(accountIndexForScenario(name, 'general_view', viewCount), index);
      assert.equal(value.maxVUs, 1);
    }
  });
}

test('잘못된 Scenario 이름과 계정 범위를 거부한다', () => {
  assert.throws(() => accountIndexForScenario('trip_creation_3', 'trip_creation', 2), /account_assignment/u);
  assert.throws(() => accountIndexForScenario('general_view_1', 'trip_creation', 2), /account_assignment/u);
});
