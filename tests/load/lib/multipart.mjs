const encoder = new TextEncoder();

function bytes(value) {
  if (typeof value === 'string') return encoder.encode(value);
  if (value instanceof Uint8Array) return value;
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  throw new TypeError('Multipart 값은 문자열, ArrayBuffer 또는 Uint8Array여야 합니다.');
}

function safeToken(value, label) {
  if (!value || /[\r\n"]/u.test(value)) {
    throw new Error(`${label}에 사용할 수 없는 문자가 있습니다.`);
  }
  return value;
}

export function buildMultipart(parts, boundary = `----yeodam-k6-${Date.now()}-${Math.random()}`) {
  safeToken(boundary, 'boundary');

  const chunks = [];
  let totalLength = 0;

  for (const part of parts) {
    const name = safeToken(part.name, 'Multipart field 이름');
    let header = `--${boundary}\r\nContent-Disposition: form-data; name="${name}"`;

    if (part.filename) {
      header += `; filename="${safeToken(part.filename, '파일명')}"`;
    }
    header += '\r\n';

    if (part.contentType) {
      header += `Content-Type: ${safeToken(part.contentType, 'Content-Type')}\r\n`;
    }
    header += '\r\n';

    const headerBytes = bytes(header);
    const dataBytes = bytes(part.data);
    const trailerBytes = bytes('\r\n');
    chunks.push(headerBytes, dataBytes, trailerBytes);
    totalLength += headerBytes.byteLength + dataBytes.byteLength + trailerBytes.byteLength;
  }

  const closingBytes = bytes(`--${boundary}--\r\n`);
  totalLength += closingBytes.byteLength;

  const body = new Uint8Array(totalLength);
  let offset = 0;
  for (const chunk of [...chunks, closingBytes]) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }

  return {
    body: body.buffer,
    contentType: `multipart/form-data; boundary=${boundary}`,
  };
}
