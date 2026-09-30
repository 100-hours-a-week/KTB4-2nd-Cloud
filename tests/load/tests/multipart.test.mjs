import assert from 'node:assert/strict';
import test from 'node:test';

import { buildMultipart } from '../lib/multipart.mjs';

test('같은 이름의 파일 필드와 일반 필드를 순서대로 구성한다', () => {
  const result = buildMultipart(
    [
      {
        name: 'attachments[]',
        filename: 'photo-0001.jpg',
        contentType: 'image/jpeg',
        data: new Uint8Array([1, 2, 3]).buffer,
      },
      {
        name: 'attachments[]',
        filename: 'photo-0002.jpg',
        contentType: 'image/jpeg',
        data: new Uint8Array([4, 5]).buffer,
      },
      { name: 'batchNo', data: '1' },
      { name: 'totalAttachmentCount', data: '2' },
      { name: 'complete', data: 'true' },
    ],
    'test-boundary',
  );

  const text = new TextDecoder('latin1').decode(result.body);
  assert.equal(result.contentType, 'multipart/form-data; boundary=test-boundary');
  assert.equal((text.match(/name="attachments\[\]"/gu) || []).length, 2);
  assert.match(text, /name="batchNo"\r\n\r\n1\r\n/u);
  assert.match(text, /name="totalAttachmentCount"\r\n\r\n2\r\n/u);
  assert.match(text, /name="complete"\r\n\r\ntrue\r\n/u);
  assert.ok(text.endsWith('--test-boundary--\r\n'));
});

test('Header injection 문자를 거부한다', () => {
  assert.throws(
    () =>
      buildMultipart([
        {
          name: 'attachments[]',
          filename: 'photo.jpg\r\nX-Test: injected',
          contentType: 'image/jpeg',
          data: new Uint8Array([1]).buffer,
        },
      ]),
    /파일명에 사용할 수 없는 문자/u,
  );
});
