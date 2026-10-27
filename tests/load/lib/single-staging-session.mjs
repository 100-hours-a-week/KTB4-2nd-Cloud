// Metadata is a preparation guard; the actual API authentication still verifies the JWT.
export function singleStagingSession(raw, baseUrl, now = Date.now(), minimumRemainingMs = 900_000) {
  const reject = () => { throw new Error('Expected one unexpired V1 staging test session; contents withheld'); };
  if (minimumRemainingMs !== null && (!Number.isFinite(minimumRemainingMs) || minimumRemainingMs < 0)) reject();
  let pool;
  try { pool = JSON.parse(raw); } catch (_) { reject(); }
  if (baseUrl !== 'https://v1-staging.yeodam-2gether.com' || pool?.baseUrl !== baseUrl ||
      pool.version !== 1 || !Array.isArray(pool.sessions) || pool.sessions.length !== 1 ||
      !Number.isFinite(Date.parse(pool.expiresAt)) || (minimumRemainingMs !== null && Date.parse(pool.expiresAt) - now < minimumRemainingMs) ||
      typeof pool.sessions[0]?.accessToken !== 'string' || !pool.sessions[0].accessToken ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(pool.sessions[0].csrfContext || '')) reject();
  return pool.sessions[0];
}

export function singleStagingAccessToken(raw, baseUrl, now = Date.now()) {
  return singleStagingSession(raw, baseUrl, now).accessToken;
}
