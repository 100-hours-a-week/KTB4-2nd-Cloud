import http from 'k6/http';
import exec from 'k6/execution';
import { Counter } from 'k6/metrics';
import { defenseConfig } from '../lib/nginx-defense.mjs';
import { summaryOutput } from '../lib/summary.mjs';

const config = defenseConfig(__ENV);
if (__ENV.K6_DEFENSE_EXECUTE !== '1') throw new Error('Execution is disabled; operator opt-in required');
const unauthorized = new Counter('nginx_probe_401');
const rejected = new Counter('nginx_probe_429');
const unexpected = new Counter('nginx_probe_unexpected');

export const options = {
  discardResponseBodies: true,
  maxRedirects: 0,
  noConnectionReuse: config.noConnectionReuse,
  scenarios: {
    repeated_map: {
      executor: 'constant-arrival-rate',
      rate: config.rate, timeUnit: '1s', duration: `${config.seconds}s`,
      preAllocatedVUs: config.preAllocatedVUs, maxVUs: config.maxVUs,
      gracefulStop: '6s',
      tags: { workload: 'unauthenticated_map', defense_run: config.runId },
    },
  },
  thresholds: {
    nginx_probe_unexpected: ['count==0'],
    dropped_iterations: ['count==0'],
    http_req_failed: ['rate==0'],
  },
};

// This callback belongs only to this unauthenticated workload. It must not be
// copied into normal user scenarios, where 401/429 are failures.
http.setResponseCallback(http.expectedStatuses(401, 429));

export function setup() {
  console.log(JSON.stringify({ event: 'nginx_probe_started', at: new Date().toISOString(), ...config }));
}

export default function () {
  unexpected.add(0);
  const response = http.get(`${config.origin}${config.path}`, {
    timeout: `${config.requestTimeoutSeconds}s`,
    headers: { 'User-Agent': 'yeodam-nginx-rehearsal' },
    tags: { name: 'GET /api/trips/map', cohort: 'replay' },
  });
  if (response.status === 401) unauthorized.add(1);
  if (response.status === 429) rejected.add(1);
  if (![401, 429].includes(response.status) || response.proto !== config.expectedProtocol) {
    unexpected.add(1);
    exec.test.abort(`Stop at unexpected status/protocol: ${response.status}/${response.proto}`);
  }
}

export function handleSummary(data) {
  return summaryOutput(data);
}
