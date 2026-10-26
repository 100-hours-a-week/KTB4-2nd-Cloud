import test from 'node:test';
import assert from 'node:assert/strict';
import { origin, createConfig, validatePool } from '../lib/nginx-create.mjs';
const env = { K6_BASE_URL: origin, K6_CREATE_CONFIRM_TARGET: origin, K6_CREATE_RUN_ID: '11111111-1111-4111-a111-111111111111' };
test('creation scope rejects production, excessive rates and unbounded duration', () => {
  assert.equal(createConfig(env).fixedVus, 200);
  for (const change of [{ K6_BASE_URL: 'https://yeodam-2gether.com' }, { K6_CREATE_CONFIRM_TARGET: '' },
    { K6_CREATE_RPS: '1000' }, { K6_CREATE_SECONDS: '3600' }, { K6_CREATE_RUN_ID: '../private' },
    { K6_CREATE_SESSION_COUNT: '100' }]) assert.throws(() => createConfig({ ...env, ...change }));
});
test('token lifetime must cover the whole bounded stage and recovery margin', () => {
  const value = { version: 1, baseUrl: origin, runId: env.K6_CREATE_RUN_ID, expiresAt: new Date(200000).toISOString(),
    sessions: [{ userId: 1, accessToken: 'private', csrfContext: 'private', csrfToken: 'private' }] };
  assert.equal(validatePool(value, 60, 0), value);
  assert.throws(() => validatePool(value, 120, 0));
  assert.throws(() => validatePool({ ...value, baseUrl: 'http://localhost' }, 60, 0));
});
