#!/usr/bin/env python3
"""Local Docker probe-policy exercise. Synthetic TLS Kafka only; no Kubernetes/AWS/GPU.

The checks use the real Connect /health endpoint and real wall-clock probe periods.
This is a kubelet policy harness, not a claim that a Kubernetes cluster was tested.
"""
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[2]


def probe_health(c, timeout):
    # Probe loopback inside the worker so disconnecting Kafka does NOT sever the
    # probe transport. This exercises /health's broker dependency, not a lost REST route.
    return c['docker']('exec', c['worker'], 'curl', '-fsS', '--max-time', str(timeout),
                       '-o', '/dev/null', 'http://127.0.0.1:8083/health', check=False).returncode == 0


def wait_healthy(c, timeout):
    deadline = time.monotonic()+90
    while not probe_health(c, timeout):
        if time.monotonic() >= deadline:
            raise AssertionError('Healthy baseline required (REST alone is insufficient)')
        time.sleep(2)


def exercise(c):
    # Read the rendered-policy input directly, with fixed approved fields checked by CI.
    text = (ROOT/'deploy/v3/connect/templates/connect.yaml').read_text()
    literal = next(line.split(': ', 1)[1] for line in text.splitlines() if line.startswith('  livenessProbe: '))
    policy = {key.strip(): int(value.strip()) for key, value in
              (entry.split(':', 1) for entry in literal.strip('{}').split(','))}
    period, timeout, failures = policy['periodSeconds'], policy['timeoutSeconds'], policy['failureThreshold']
    assert (period, timeout, failures) == (10, 5, 18)
    worker, net, docker = c['worker'], c['net'], c['docker']
    results = {'harness': 'real Docker /health with kubelet consecutive-failure policy; not Kubernetes runtime',
               'policy': policy, 'transientFaultSeconds': 90, 'samples': []}
    initial = json.loads(docker('inspect', worker).stdout)[0]['State']['StartedAt']
    consecutive = maximum = 0
    disconnected = stopped = False
    try:
        wait_healthy(c, timeout)
        docker('network', 'disconnect', net, worker); disconnected = True
        start = time.monotonic(); next_probe = start
        while time.monotonic() - start < 165:
            if disconnected and time.monotonic() - start >= 90:
                docker('network', 'connect', '--alias', 'connect-check', net, worker); disconnected = False
                results['reconnectedAtSeconds'] = time.monotonic() - start
            if time.monotonic() < next_probe:
                time.sleep(min(.2, next_probe-time.monotonic())); continue
            ok = probe_health(c, timeout)
            consecutive = 0 if ok else consecutive+1; maximum = max(maximum, consecutive)
            results['samples'].append({'phase': 'transient', 'atSeconds': round(time.monotonic()-start,3), 'healthy': ok, 'consecutiveFailures': consecutive})
            assert consecutive < failures, 'Transient outage exhausted reviewed Liveness policy'
            if not disconnected and ok:break
            next_probe += period
        assert not disconnected and ok, 'Connect failed to recover within the transient observation window'
        state = json.loads(docker('inspect', worker).stdout)[0]
        assert state['State']['StartedAt'] == initial and not state['State']['OOMKilled']
        assert maximum > 0, 'Fault did not affect the real health endpoint'
        results['transientRecoveredWithoutRestart'] = True
        results['maximumConsecutiveFailures'] = maximum
        print('PASS: 90s broker disconnect recovered with unchanged container start; testing bounded persistent hang next', flush=True)
        # Suspend the real local process to prove this policy does not tolerate a hang forever.
        docker('kill', '--signal=STOP', worker); stopped = True
        start = time.monotonic(); consecutive = 0
        for i in range(failures):
            target = start+i*period
            if target > time.monotonic():time.sleep(target-time.monotonic())
            ok = probe_health(c, timeout)
            consecutive = 0 if ok else consecutive+1
            results['samples'].append({'phase': 'persistent_unresponsive', 'atSeconds': round(time.monotonic()-start,3), 'healthy': ok, 'consecutiveFailures': consecutive})
            assert not ok, 'Suspended worker unexpectedly passed health'
        assert consecutive == failures
        results['persistentRestartDecisionAfterSeconds'] = time.monotonic()-start
        docker('kill', '--signal=CONT', worker); stopped = False
        # Apply the harness's restart decision to this synthetic container only.
        docker('restart', worker, timeout=120)
        fresh = json.loads(docker('inspect', worker).stdout)[0]
        assert fresh['State']['StartedAt'] != initial
        port = fresh['NetworkSettings']['Ports']['8083/tcp'][0]['HostPort']
        c['wait_api'](port)
        wait_healthy(c, timeout)
        results['persistentHangRestartRecovered'] = True
        (c['output']/'recovery-policy-result.json').write_text(json.dumps(results, indent=2))
        print('PASS: real 90s Kafka disconnect recovered without worker restart; persistent SIGSTOP reached bounded restart and recovered', flush=True)
    finally:
        if stopped:docker('kill', '--signal=CONT', worker, check=False)
        if disconnected:docker('network', 'connect', '--alias', 'connect-check', net, worker, check=False)
        (c['output']/'recovery-policy-observation.json').write_text(json.dumps(results, indent=2))
