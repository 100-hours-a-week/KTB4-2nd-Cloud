import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { sanitize, failureStage } from '../scripts/sanitize-nginx-mobile-har.mjs';

const origin = 'https://v1-staging.yeodam-2gether.com';
const rid = 'a'.repeat(32);
const entry = (url, status = 200, extras = {}) => ({
  startedDateTime: '2026-10-27T16:00:00Z', time: 10, timings: { dns: -1, connect: -1, ssl: -1 },
  request: { url, method: 'GET', cookies: ['PRIVATE_COOKIE'], postData: { text: 'PRIVATE_BODY' } },
  response: { status, httpVersion: 'HTTP/2', headers: [{ name: 'x-request-id', value: rid },
    { name: 'set-cookie', value: 'PRIVATE_COOKIE' }], content: { text: 'PRIVATE_BODY' } }, ...extras,
});
const har = entries => ({ log: { entries } });

test('OAuth navigation metadata survives, credentials and arbitrary path/query content do not', () => {
  const start = entry(origin + '/api/auth/kakao/authorize?code=PRIVATE_CODE', 302);
  start.response.headers.push({ name: 'Location', value: 'https://kauth.kakao.com/oauth/authorize?client_secret=PRIVATE_SECRET' });
  const result = sanitize(har([start, entry('https://kauth.kakao.com/oauth/authorize?state=PRIVATE_STATE'),
    entry('https://accounts.kakao.com/login?email=PRIVATE_EMAIL'), entry(origin + '/api/auth/kakao/callback?code=PRIVATE_CODE'),
    entry(origin + '/api/auth/token/exchange'), entry(origin + '/api/trips/map'),
    entry('https://accounts.kakao.com/users/PRIVATE_PERSON'), entry(origin + '/user/PRIVATE_PERSON'),
    entry('https://third-party.test/PRIVATE_PERSON')]));
  assert.deepEqual(result.missingAuthPhases, []);
  assert.equal(result.requests[0].redirectTo, 'https://kauth.kakao.com/oauth/authorize');
  assert.equal(result.requests[6].url, 'https://accounts.kakao.com/[other]');
  assert.equal(result.requests[7].url, origin + '/[other]');
  assert.equal(result.omittedOutsideTarget, 1);
  assert.doesNotMatch(JSON.stringify(result), /PRIVATE_|client_secret|email=|state=|code=|set-cookie|third-party\.test/);
});

test('map-only HAR reports missing earlier auth; status zero and unavailable timings remain unknown', () => {
  const x = entry(origin + '/api/trips/map', 0); delete x.time; delete x.timings;
  const result = sanitize(har([x]));
  assert.deepEqual(result.missingAuthPhases, ['authorize', 'provider', 'callback', 'exchange']);
  assert.equal(result.requests[0].failureStage, null);
  assert.equal(result.requests[0].timingsMs.ssl, null);
  assert.equal(result.requests[0].outcome, 'no-http-response-recorded');
});

test('stage classification separates policy and cancel from network-stage errors', () => {
  for (const [code, stage] of [['ERR_NAME_NOT_RESOLVED', 'dns'], ['ERR_CONNECTION_REFUSED', 'tcp-connect'],
    ['ERR_CERT_AUTHORITY_INVALID', 'tls'], ['ERR_BLOCKED_BY_CLIENT', 'browser-policy'],
    ['ERR_ABORTED', 'client-canceled'], ['ERR_CONNECTION_TIMED_OUT', 'transport-stage-unknown']]) {
    assert.equal(failureStage(code), stage);
    const e = entry(origin + '/login', 0, { _error: code + ' PRIVATE_ERROR_BODY' });
    assert.equal(sanitize(har([e])).requests[0].failureStage, stage);
    assert.doesNotMatch(JSON.stringify(sanitize(har([e]))), /PRIVATE_ERROR_BODY/);
  }
});

test('relative redirect is resolved; unknown origins/invalid URLs are excluded; negative timing is preserved', () => {
  const x = entry(origin + '/login', 302); x.response.redirectURL = '/auth/callback?code=PRIVATE_CODE';
  const result = sanitize(har([x, entry('relative-invalid'), entry('https://accounts.kakao.com.evil.test/login')]));
  assert.equal(result.requests.length, 1);
  assert.equal(result.requests[0].redirectTo, origin + '/auth/callback');
  assert.equal(result.requests[0].timingsMs.connect, -1);
  assert.throws(() => sanitize(har(Array(10001).fill(x))));
});

test('CLI keeps raw bytes private and refuses output overwrite or credential-bearing JSON error text', async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'har452-'));
  try {
    const input = path.join(dir, 'private.har'), out = path.join(dir, 'redacted.json');
    await fs.writeFile(input, JSON.stringify(har([entry(origin + '/login?token=PRIVATE_TOKEN')])));
    const script = new URL('../scripts/sanitize-nginx-mobile-har.mjs', import.meta.url);
    const run = () => spawnSync(process.execPath, [script.pathname, input, out], { encoding: 'utf8' });
    assert.equal(run().status, 0);
    assert.equal((await fs.stat(out)).mode & 0o777, 0o600);
    const before = await fs.readFile(out, 'utf8');
    assert.equal(run().status, 1); assert.equal(await fs.readFile(out, 'utf8'), before);
    await fs.writeFile(input, '{"PRIVATE_TOKEN"');
    const failed = run(); assert.equal(failed.status, 1);
    assert.doesNotMatch(failed.stderr + before, /PRIVATE_TOKEN/);
  } finally { await fs.rm(dir, { recursive: true, force: true }); }
});
