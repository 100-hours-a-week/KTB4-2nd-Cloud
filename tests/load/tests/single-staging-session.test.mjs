import assert from 'node:assert/strict';
import test from 'node:test';
import { singleStagingAccessToken } from '../lib/single-staging-session.mjs';
const origin = 'https://v1-staging.yeodam-2gether.com';
const now = Date.parse('2026-10-24T01:00:00Z');
const pool = {version: 1, baseUrl: origin, expiresAt: '2026-10-24T01:30:00Z', sessions: [{accessToken:'private-test-token',csrfContext:'11111111-1111-4111-a111-111111111111'}]};
test('one new session is usable without placing its token in an environment argument', () => {
  assert.equal(singleStagingAccessToken(JSON.stringify(pool), origin, now), 'private-test-token');
});
test('wrong origin, multiple sessions, short remaining life and malformed contents are refused safely', () => {
  for (const p of [{...pool,baseUrl:'https://yeodam-2gether.com'}, {...pool,sessions:[...pool.sessions,...pool.sessions]},
    {...pool,expiresAt:'2026-10-24T01:14:59Z'}, {...pool,sessions:[{}]}, {...pool,expiresAt:'bad'}]) {
    assert.throws(()=>singleStagingAccessToken(JSON.stringify(p), origin, now), e => !e.message.includes('private-test-token'));
  }
  assert.throws(()=>singleStagingAccessToken('{private-test-token', origin, now));
  assert.throws(()=>singleStagingAccessToken(JSON.stringify(pool),'https://yeodam-2gether.com',now));
});
