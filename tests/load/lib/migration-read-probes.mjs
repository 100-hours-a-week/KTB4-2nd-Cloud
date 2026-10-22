import http from 'k6/http';
import exec from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';

export function createReadProbes(base, runId, pool, fixtures) {
  const enabled = fixtures !== null;
  if (enabled && (fixtures.runId !== runId || fixtures.fixtures.length !== 20 ||
      fixtures.fixtures.some(f => !Number.isInteger(f.tripId) || f.tripId < 1 ||
        !Number.isInteger(f.poolIndex) || f.poolIndex < 120 || f.poolIndex >= 140 ||
        pool[f.poolIndex + 1]?.userId !== f.userId))) {
    throw new Error('Read fixtures do not match the 30 RPS actor pool');
  }
  const metrics = enabled ? Object.fromEntries(['list', 'detail'].map(kind => [kind, {
    requests: new Counter(`b_read_${kind}_requests`),
    ok: new Rate(`b_read_${kind}_ok`),
    latency: new Trend(`b_read_${kind}_ms`, true),
  }])) : {};
  const scenario = kind => ({ executor: 'constant-arrival-rate', exec: kind === 'list' ? 'readList' : 'readDetail',
    rate: 1, timeUnit: '1s', duration: '900s', preAllocatedVUs: 5, maxVUs: 120, gracefulStop: '105s' });
  function request(kind) {
    const fixture = fixtures.fixtures[exec.scenario.iterationInTest % fixtures.fixtures.length];
    const session = pool[fixture.poolIndex + 1];
    const at = Date.now();
    const response = http.get(base + (kind === 'list' ? '/api/trips' : `/api/trips/${fixture.tripId}`), {
      cookies: { accessToken: { value: session.accessToken }, CSRF_CONTEXT: { value: session.csrfContext } },
      timeout: '100s', redirects: 0, responseCallback: http.expectedStatuses(200),
      tags: { operation: `read_${kind}` },
    });
    let valid = false;
    if (response.status === 200) {
      try {
        const data = response.json().data;
        valid = kind === 'list' ? Array.isArray(data?.items) : data?.tripId === fixture.tripId;
      } catch (_) { /* Preserve malformed responses as failures. */ }
    }
    const elapsedMs = Date.now() - at;
    metrics[kind].requests.add(1); metrics[kind].ok.add(valid); metrics[kind].latency.add(elapsedMs);
    // No response body, token, signed URL or cookie is written to the event stream.
    console.log(JSON.stringify({ source: 'migration-b406', event: 'read_response', runId,
      kind, at, elapsedMs, status: response.status, ok: valid,
      connectingMs: response.timings.connecting, tlsMs: response.timings.tls_handshaking }));
  }
  return { scenarios: enabled ? { read_list: scenario('list'), read_detail: scenario('detail') } : {},
    thresholds: enabled ? { b_read_list_ok: ['rate==1'], b_read_detail_ok: ['rate==1'] } : {},
    list: () => request('list'), detail: () => request('detail') };
}
