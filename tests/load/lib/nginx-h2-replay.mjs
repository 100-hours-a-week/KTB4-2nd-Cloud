export const origin = 'https://v1-staging.yeodam-2gether.com';
export const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function config(env, profile) {
  const vus = Number(env.K6_H2_VUS);
  const seconds = Number(env.K6_H2_SECONDS);
  const rate = Number(env.K6_H2_RATE || 0);
  if (env.K6_BASE_URL !== origin || env.K6_H2_CONFIRM_TARGET !== origin ||
      !uuid.test(env.K6_H2_RUN_ID || '') || ![1, 16, 64, 128, 141].includes(vus) ||
      ![1, 30, 299].includes(seconds) || ![0, 1, 64, 256].includes(rate)) {
    throw new Error('Exact staging identity and fixed bounded #442 stages required');
  }
  if (!Array.isArray(profile.httpCompletionProfile) || profile.httpCompletionProfile.length !== 299 ||
      profile.observedRows !== 133875 || profile.scope.indexOf('Server HTTP completion profile only') !== 0) {
    throw new Error('Exact saved instructor server-completion profile required');
  }
  const shape = profile.httpCompletionProfile.map((r, index) => {
    if (r.relativeSecond !== index || !Number.isInteger(r.completedHttpRequests) ||
        r.completedHttpRequests < 0 || r.completedHttpRequests > 564) throw new Error('Invalid profile bin');
    return r.completedHttpRequests;
  });
  if (shape.reduce((a, b) => a + b, 0) !== 133875 ||
      (rate === 0 && (![128, 141].includes(vus) || seconds !== 299))) throw new Error('Profile stage mismatch');
  const expected = rate ? rate * seconds : 133875;
  return { origin, vus, seconds, rate, shape, expected, runId: env.K6_H2_RUN_ID,
    // Initial HTTP connection observation was <=56/s; this is a chosen warmup bound, not measured SYN rate.
    warmupSeconds: Math.ceil(vus / 20) + 3, warmupOpeningRate: 20 };
}

export function slots(count, workers, index, offset = 0) {
  const result = [];
  const first = (index - (offset % workers) + workers) % workers;
  for (let i = first; i < count; i += workers) result.push(i);
  return result;
}
