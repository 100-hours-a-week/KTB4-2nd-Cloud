import assert from 'node:assert/strict';
import test from 'node:test';
import { defenseConfig, stagingOrigin } from '../lib/nginx-defense.mjs';

const env = {
  K6_BASE_URL: stagingOrigin, K6_DEFENSE_CONFIRM_TARGET: stagingOrigin,
  K6_DEFENSE_RUN_ID: 'local-fixture', K6_DEFENSE_RPS: '1', K6_DEFENSE_DURATION_SECONDS: '10',
  K6_DEFENSE_PREALLOCATED_VUS: '10', K6_DEFENSE_MAX_VUS: '10',
  K6_DEFENSE_NO_CONNECTION_REUSE: 'false', K6_DEFENSE_EXPECTED_PROTOCOL: 'HTTP/1.1',
};

test('finite plan explicitly records reuse, protocol and target without a request', () => {
  const plan = defenseConfig(env);
  assert.equal(plan.rate, 1);
  assert.equal(plan.seconds, 10);
  assert.equal(plan.noConnectionReuse, false);
  assert.equal(plan.path, '/api/trips/map');
});

test('production, alternate origins, implicit parameters and excessive budgets are rejected', () => {
  const invalid = [
    { K6_BASE_URL: 'https://yeodam-2gether.com' },
    { K6_BASE_URL: `${stagingOrigin}/` },
    { K6_DEFENSE_CONFIRM_TARGET: '' }, { K6_DEFENSE_RUN_ID: '../private' },
    { K6_DEFENSE_RPS: '101' }, { K6_DEFENSE_RPS: '' }, { K6_DEFENSE_RPS: '1.5' },
    { K6_DEFENSE_DURATION_SECONDS: '301' }, { K6_DEFENSE_MAX_VUS: '513' },
    { K6_DEFENSE_PREALLOCATED_VUS: '11' },
    { K6_DEFENSE_NO_CONNECTION_REUSE: '' }, { K6_DEFENSE_EXPECTED_PROTOCOL: '' },
  ];
  for (const change of invalid) assert.throws(() => defenseConfig({ ...env, ...change }));
  assert.equal(defenseConfig({ ...env, K6_DEFENSE_NO_CONNECTION_REUSE: 'true' }).noConnectionReuse, true);
});
