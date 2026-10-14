export function uuidFromHex(hex) {
  if (!/^[0-9a-f]{64}$/.test(hex)) throw new Error('Expected SHA-256 hex');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-a${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

export function retryableStatus(status) {
  return [0, 408, 429, 500, 502, 503, 504].includes(status);
}

export function confirmedResponse(status, body, request) {
  const data = body?.data;
  return data?.runId === request.runId && data?.requestId === request.requestId &&
    ((status === 201 && data.result === 'created') || (status === 200 && data.result === 'already_exists'));
}
