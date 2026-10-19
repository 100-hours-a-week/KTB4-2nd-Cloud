// Deliberately separate from production load defaults. Limits are preparation caps,
// not measured service capacity. No network access occurs in this module.
export const stagingOrigin = 'https://v1-staging.yeodam-2gether.com';

function integer(env, key, maximum) {
  const raw = env[key];
  if (!/^[1-9][0-9]*$/.test(raw || '')) throw new Error(`${key}: positive integer required`);
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value > maximum) throw new Error(`${key}: maximum ${maximum}`);
  return value;
}

export function defenseConfig(env) {
  if (env.K6_BASE_URL !== stagingOrigin || env.K6_DEFENSE_CONFIRM_TARGET !== stagingOrigin) {
    throw new Error('Both target fields must exactly match the V1 staging origin');
  }
  const runId = env.K6_DEFENSE_RUN_ID || '';
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(runId)) throw new Error('Explicit safe run ID required');
  const rate = integer(env, 'K6_DEFENSE_RPS', 100);
  const seconds = integer(env, 'K6_DEFENSE_DURATION_SECONDS', 300);
  const preAllocatedVUs = integer(env, 'K6_DEFENSE_PREALLOCATED_VUS', 512);
  const maxVUs = integer(env, 'K6_DEFENSE_MAX_VUS', 512);
  if (preAllocatedVUs > maxVUs) throw new Error('Preallocated VUs exceed maximum');
  if (!['true', 'false'].includes(env.K6_DEFENSE_NO_CONNECTION_REUSE)) {
    throw new Error('Explicit true/false connection reuse policy required');
  }
  if (!['HTTP/1.1', 'HTTP/2.0'].includes(env.K6_DEFENSE_EXPECTED_PROTOCOL)) {
    throw new Error('Explicit HTTP protocol cohort required');
  }
  return {
    origin: stagingOrigin, path: '/api/trips/map', runId, rate, seconds,
    preAllocatedVUs, maxVUs,
    noConnectionReuse: env.K6_DEFENSE_NO_CONNECTION_REUSE === 'true',
    expectedProtocol: env.K6_DEFENSE_EXPECTED_PROTOCOL,
    requestTimeoutSeconds: 5,
  };
}
