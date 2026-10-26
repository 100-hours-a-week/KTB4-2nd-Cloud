export const origin = 'https://v1-staging.yeodam-2gether.com';
export const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function createConfig(env) {
  if (env.K6_BASE_URL !== origin || env.K6_CREATE_CONFIRM_TARGET !== origin) throw new Error('Exact V1 staging confirmation required');
  if (!uuidPattern.test(env.K6_CREATE_RUN_ID || '')) throw new Error('Canonical run UUID required');
  const mode = env.K6_CREATE_MODE || 'rate';
  const rate = Number(env.K6_CREATE_RPS || 1);
  const seconds = Number(env.K6_CREATE_SECONDS || 60);
  const sessionCount = Number(env.K6_CREATE_SESSION_COUNT || 1);
  if (!['single', 'burst', 'rate'].includes(mode) || ![1, 5, 10, 20, 30, 50, 75, 100].includes(rate) ||
      ![60, 120].includes(seconds) || ![1, 3].includes(sessionCount)) throw new Error('Unapproved measurement condition');
  if (!['false', 'true'].includes(env.K6_CREATE_NO_REUSE || 'false')) throw new Error('Explicit connection reuse boolean required');
  if (!['false', 'true'].includes(env.K6_CREATE_EXPECT_REJECTION || 'false')) throw new Error('Explicit rejection expectation required');
  return { origin, runId: env.K6_CREATE_RUN_ID, mode, rate, seconds, sessionCount,
    noReuse: env.K6_CREATE_NO_REUSE === 'true', expectRejection: env.K6_CREATE_EXPECT_REJECTION === 'true', fixedVus: 200 };
}

export function validatePool(value, seconds, now = Date.now()) {
  if (value.version !== 1 || value.baseUrl !== origin || !uuidPattern.test(value.runId || '') ||
      !Number.isFinite(Date.parse(value.expiresAt)) || Date.parse(value.expiresAt) <= now + (seconds + 90) * 1000 ||
      !Array.isArray(value.sessions) || !value.sessions.length) throw new Error('Pool identity or TTL invalid');
  for (const s of value.sessions) {
    if (!Number.isInteger(s.userId) || !s.accessToken || !s.csrfContext || !s.csrfToken) throw new Error('Incomplete synthetic session');
  }
  return value;
}
