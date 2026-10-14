import test from 'node:test';
import assert from 'node:assert/strict';
import { confirmedResponse, retryableStatus, uuidFromHex } from '../lib/migration-probe.mjs';

test('success needs the exact request identity and commit result', () => {
  const request = { runId: 'run', requestId: 'request' };
  assert.equal(confirmedResponse(201, { data: { ...request, result: 'created' } }, request), true);
  assert.equal(confirmedResponse(200, { data: { ...request, result: 'already_exists' } }, request), true);
  assert.equal(confirmedResponse(201, { data: { ...request, requestId: 'wrong', result: 'created' } }, request), false);
  assert.equal(confirmedResponse(201, { data: { ...request, result: 'queued' } }, request), false);
});
test('authentication and contract failures are never retried', () => {
  for (const status of [400, 401, 403, 404, 409, 413]) assert.equal(retryableStatus(status), false);
  for (const status of [0, 408, 429, 500, 502, 503, 504]) assert.equal(retryableStatus(status), true);
});
test('deterministic UUID preserves stable retry identity and valid format', () => {
  const id = uuidFromHex('0123456789abcdef'.repeat(4));
  assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-a[0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.equal(id, uuidFromHex('0123456789abcdef'.repeat(4)));
});
