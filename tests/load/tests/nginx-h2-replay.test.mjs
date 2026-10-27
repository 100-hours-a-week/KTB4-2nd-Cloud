import test from 'node:test';
import assert from 'node:assert/strict';
import { config, slots, origin } from '../lib/nginx-h2-replay.mjs';

const profile = { observedRows: 133875, scope: 'Server HTTP completion profile only',
  httpCompletionProfile: Array.from({ length: 299 }, (_, i) => ({ relativeSecond: i,
    completedHttpRequests: i < 222 ? 448 : 447 })) };
// Build an exact-count bounded fixture; it checks the contract, not the archived time series.
profile.httpCompletionProfile[0].completedHttpRequests += 133875 - profile.httpCompletionProfile.reduce((s, r) => s + r.completedHttpRequests, 0);
const env = { K6_BASE_URL: origin, K6_H2_CONFIRM_TARGET: origin,
  K6_H2_RUN_ID: 'df21f31a-7447-4b22-b41c-9b5753cfa382', K6_H2_VUS: '141', K6_H2_SECONDS: '299' };

test('round robin slots partition every request exactly once including empty bins', () => {
  for (const count of [0, 1, 35, 481, 564]) {
    const all = Array.from({ length: 141 }, (_, i) => slots(count, 141, i)).flat();
    assert.equal(all.length, count);
    assert.equal(new Set(all).size, count);
    assert.ok(all.every(x => x >= 0 && x < count));
  }
});
test('production, excessive VUs and tampered replay profiles fail before network', () => {
  assert.throws(() => config({ ...env, K6_BASE_URL: 'https://yeodam-2gether.com' }, profile));
  assert.throws(() => config({ ...env, K6_H2_VUS: '1000' }, profile));
  const broken = structuredClone(profile); broken.httpCompletionProfile[0].completedHttpRequests = 999;
  assert.throws(() => config(env, broken));
});
test('exact saved profile uses 141 client workers without claiming 141 live sockets', () => {
  const c = config(env, profile);
  assert.equal(c.expected, 133875);
  assert.equal(c.vus, 141);
  assert.equal(c.warmupOpeningRate, 20);
});
test('rotating allocation keeps total requests per connection within one request', () => {
  const counts = Array(141).fill(0);
  let offset = 141;
  for (const n of [481, 564, 35, 564, 481]) {
    for (let i = 0; i < counts.length; i++) counts[i] += slots(n, 141, i, offset).length;
    offset += n;
  }
  assert.ok(Math.max(...counts) - Math.min(...counts) <= 1);
});
