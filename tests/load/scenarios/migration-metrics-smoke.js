// Connectivity check only: emits known values and sends no service HTTP requests.
import { sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';
const started = new Counter('probe_started');
const confirmed = new Counter('probe_confirmed');
const elapsed = new Trend('probe_end_to_end', true);
export const options = {
  vus: 1, iterations: 10,
  tags: { kind: 'connectivity_check', testid: 'metrics-check', generator: 'connectivity-check' },
};
export default function () {
  started.add(1); confirmed.add(1); elapsed.add(1000); sleep(1);
}
