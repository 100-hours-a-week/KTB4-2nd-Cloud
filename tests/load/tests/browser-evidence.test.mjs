import test from 'node:test';
import assert from 'node:assert/strict';
import { safeUrl, safeText, sanitizeEvents, har, failureStage } from '../lib/browser-evidence.mjs';

test('URL query, userinfo and S3 object names are not evidence output', () => {
  assert.equal(safeUrl('https://user:secret@a.test/api?token=private#x'), 'https://a.test/api');
  assert.equal(safeUrl('https://bucket.s3.amazonaws.com/private/photo.jpg?X-Amz-Signature=x'),
    'https://bucket.s3.amazonaws.com/[object]');
  assert.equal(safeText('Bearer abc accessToken=secret eyJabc.def.ghi'), 'Bearer [redacted] accessToken=[redacted] [jwt]');
});
test('CDP evidence excludes auth headers and response/request bodies', () => {
  const report = sanitizeEvents([
    { method:'Network.requestWillBeSent', params:{ requestId:'r', timestamp:1, wallTime:100,
      request:{ url:'https://a.test/api?code=secret', method:'POST', headers:{Cookie:'secret'}, postData:'secret'} } },
    { method:'Network.responseReceived', params:{ requestId:'r', response:{status:200, protocol:'h2',
      headers:{'Set-Cookie':'secret','x-request-id':'server-id'}, timing:{dnsStart:-1,dnsEnd:-1,connectStart:-1,connectEnd:-1,sslStart:-1,sslEnd:-1,sendStart:0,sendEnd:1,receiveHeadersStart:2} } } },
    { method:'Network.loadingFinished', params:{requestId:'r',timestamp:1.1} },
  ]);
  assert.ok(!JSON.stringify(report).includes('secret'));
  assert.equal(report.requests[0].timing.ssl, -1);
  assert.equal(report.requests[0].requestId, 'server-id');
  assert.equal(har(report).log.entries[0].request.headers.length, 0);
});
test('browser blocking and canceled requests remain distinguishable', () => {
  const report = sanitizeEvents([
    { method:'Network.requestWillBeSent',params:{requestId:'r',timestamp:1,wallTime:100,request:{url:'https://a.test/',method:'GET'}} },
    { method:'Network.loadingFailed',params:{requestId:'r',timestamp:2,errorText:'net::ERR_BLOCKED_BY_CLIENT',blockedReason:'inspector',canceled:false} },
  ]);
  assert.equal(report.requests[0].finished, false);
  assert.equal(report.requests[0].blockedReason, 'inspector');
  assert.equal(report.requests[0].durationMs, 1000);
  assert.equal(failureStage(report.requests[0]), 'browser-policy');
});
test('ambiguous transport failures are not mislabeled as TLS', () => {
  assert.equal(failureStage({finished:false,error:'net::ERR_CONNECTION_TIMED_OUT'}), 'transport-stage-unknown');
  assert.equal(failureStage({finished:false,error:'net::ERR_CERT_DATE_INVALID'}), 'tls');
  assert.equal(failureStage({finished:false,error:'net::ERR_NAME_NOT_RESOLVED'}), 'dns');
  assert.equal(failureStage({finished:false,error:'net::ERR_CONNECTION_REFUSED'}), 'tcp');
  assert.equal(failureStage({finished:false,canceled:true}), 'client-canceled');
  assert.equal(failureStage({finished:true,status:429}), 'http');
  assert.equal(failureStage({}), 'pending');
});
