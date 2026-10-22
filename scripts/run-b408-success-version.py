#!/usr/bin/env python3
"""Replay the successful #391 implementation with only dump pacing added."""
import argparse
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT.parent / 'KTB4-2nd-Cloud-391'
PIN = '2a2907f'
IMAGE = 'ghcr.io/100-hours-a-week/yeodam-backend@sha256:d39fd16fc92e77d9c51be9056a2d0c79e355fdf0ad81e38a8d463ab554fd2010'
EVIDENCE = ROOT.parent / 'KTB4-2nd-Cloud/.local-docs/evidence/408-snapshot-contention/success391-limited'
RATE = 7276913
CASES = {'success391-limited': RATE, 'success391-limited-9mb': 9000000,
         'success391-9mb-wait-30': 9000000, 'success391-9mb-wait-10': 9000000,
         'success391-9mb-wait-50': 9000000}


def load():
    head = subprocess.check_output(['git', '-C', str(LEGACY), 'rev-parse', 'HEAD'], text=True).strip()
    pin = subprocess.check_output(['git', '-C', str(LEGACY), 'rev-parse', PIN], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(LEGACY), 'status', '--porcelain'], text=True)
    if head != pin or dirty:
        raise RuntimeError('The successful #391 checkout must remain pinned and clean')
    spec = importlib.util.spec_from_file_location('success391', LEGACY / 'scripts/b-integration376-run.py')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.EVIDENCE = m.ssm.EVIDENCE = EVIDENCE
    return m


def render_bulk(template):
    """Wait for natural client release; retain the exact import exclusion predicate."""
    guard = next(line for line in template.splitlines() if "raise RuntimeError('Target has unexpected clients')" in line)
    predicate = guard.strip().split("if ", 1)[1].split("!='0':", 1)[0]
    replacement = """    client_wait_started = time.monotonic()
    while QUERY != '0':
        if time.monotonic() - client_wait_started >= 45:
            raise RuntimeError('Target has unexpected clients after45 seconds; import not started')
        time.sleep(0.5)
    print(json.dumps({'phase': 'target_clients_released', 'waitSeconds': time.monotonic() - client_wait_started}), flush=True)""".replace('QUERY', predicate)
    if template.count(guard) != 1 or 'import time' in template:
        raise RuntimeError('Unexpected pinned bulk template')
    return template.replace('import subprocess', 'import subprocess\nimport time', 1).replace(guard, replacement, 1)


def initialize(rate=RATE, rps=30, wait_clients=False):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / 'plan.json'
    if path.exists():
        raise RuntimeError('Run already initialized; preserve evidence')
    old = json.loads((ROOT.parent / 'KTB4-2nd-Cloud/.local-docs/evidence/391-b-integration/plan.json').read_text())
    baseline = next(case for case in old['rates'] if case['rate'] == rps)
    case = {**baseline, 'runId': str(uuid.uuid4())}
    old.update(rates=[case], snapshotBytesPerSecond=rate, baselineCloudCommit=PIN,
               comparisonBaselineRun=baseline['runId'],
               observationOnly='Source proc CPU sampler; no BE instrumentation or added GET load',
               historicalDataRows=2021037 if rps == 30 else None, dataNotReset=True)
    old['bulkClientWaitSeconds'] = 45 if wait_clients else 0
    assert old['backendImage'] == IMAGE
    path.write_text(json.dumps(old, indent=2))


def instrument(m, case):
    """Keep the pinned runner intact; intercept only its snapshot submission."""
    original_aws, original_wait, original_log = m.ssm.aws, m.wait, m.log
    observation = {}
    def save():
        (EVIDENCE / 'observation.json').write_text(json.dumps(observation, indent=2))
    def aws(*args):
        if args[:2] == ('ssm', 'send-command') and '--cli-input-json' in args:
            request = json.loads(Path(args[args.index('--cli-input-json') + 1].removeprefix('file://')).read_text())
            if request.get('DocumentName') == 'yeodam-v2-staging-mysql-source-snapshot':
                if 'snapshotCommand' in observation:
                    raise RuntimeError('Do not capture a second snapshot for this Run')
                spec = importlib.util.spec_from_file_location('pace408', ROOT / 'scripts/b-integration408-snapshot.py')
                pacing = importlib.util.module_from_spec(spec); spec.loader.exec_module(pacing)
                template = (LEGACY / 'terraform/v2/staging/files/capture-mysql-replication-source.sh').read_text()
                shell = pacing.render(template, case['runId'], case.get('snapshotBytesPerSecond', RATE))
                data = base64.b64encode((ROOT / 'scripts/b-integration408-snapshot.py').read_bytes()).decode()
                commands = ["printf '%s' '" + data + "' | base64 -d > /run/yeodam-b391/b-integration408-snapshot.py",
                            'export BACKUP_BUCKET=yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2',
                            "bash -s <<'SNAPSHOT408'\n" + shell + '\nSNAPSHOT408']
                cid = m.raw('source', commands, sync=False)
                observation['snapshotCommand'] = cid; save()
                return {'Command': {'CommandId': cid}}
        return original_aws(*args)
    def wait(host, cid, *args, **kwargs):
        result = original_wait(host, cid, *args, **kwargs)
        if cid == observation.get('snapshotCommand'):
            observation['snapshotTiming'] = {k: result.get(k) for k in ('ExecutionStartDateTime', 'ExecutionEndDateTime')}
            save()
        return result
    def log(phase, **values):
        original_log(phase, **values)
        if phase == 'load_started':
            observation['startedAt'] = values['startedAt']
            command = ('python3 /run/yeodam-b391/b-integration393-resources.py --run-id ' + case['runId']
                       + ' --started-at ' + str(values['startedAt']))
            observation['resourceSamplerCommand'] = m.raw('source', [command], sync=False, bound=1000)
            save()
    m.ssm.aws, m.wait, m.log = aws, wait, log
    # Cleanup API batching only: the number of old STOPPED ECS tasks now exceeds100.
    def stop_service(old):
        tasks = list(set(m.ssm.aws('ecs', 'list-tasks', '--cluster', m.CLUSTER, '--service-name', m.SERVICE,
                                 '--desired-status', 'RUNNING')['taskArns'] +
                         m.ssm.aws('ecs', 'list-tasks', '--cluster', m.CLUSTER, '--service-name', m.SERVICE,
                                   '--desired-status', 'STOPPED')['taskArns']))
        m.ssm.aws('ecs', 'update-service', '--cluster', m.CLUSTER, '--service', m.SERVICE,
                  '--task-definition', old, '--desired-count', '0')
        end = time.monotonic() + 420
        while time.monotonic() < end:
            service = m.ssm.aws('ecs', 'describe-services', '--cluster', m.CLUSTER, '--services', m.SERVICE)['services'][0]
            stopped = all(t['lastStatus'] == 'STOPPED' for offset in range(0, len(tasks), 100)
                          for t in m.ssm.aws('ecs', 'describe-tasks', '--cluster', m.CLUSTER,
                                            '--tasks', *tasks[offset:offset + 100])['tasks'])
            if service['runningCount'] == 0 and stopped:
                return
            time.sleep(3)
        raise RuntimeError('E2E task still stopping; preserve Target')
    m.service_stop = stop_service


def main():
    global EVIDENCE
    p = argparse.ArgumentParser(); p.add_argument('action', choices=('init', 'deploy', 'execute'))
    p.add_argument('--case', choices=CASES, default='success391-limited'); args = p.parse_args()
    EVIDENCE = EVIDENCE.parent / args.case
    if args.action == 'init':
        initialize(CASES[args.case], int(args.case.rsplit('-', 1)[-1]) if '-wait-' in args.case else 30, '-wait-' in args.case); return
    m = load()
    if m.ssm.aws('sts', 'get-caller-identity')['Account'] != '483175530259':
        raise RuntimeError('Wrong account')
    plan = json.loads((EVIDENCE / 'plan.json').read_text())
    assert plan['backendImage'] == IMAGE and plan['snapshotBytesPerSecond'] == CASES[args.case]
    if args.action == 'deploy':
        m.deploy()
        if plan.get('bulkClientWaitSeconds') == 45:
            template = (LEGACY / 'scripts/b-integration376-bulk.py').read_text()
            patched = render_bulk(template)
            data = base64.b64encode(patched.encode()).decode()
            digest = hashlib.sha256(patched.encode()).hexdigest()
            m.raw('target', ["printf '%s' '" + data + "' | base64 -d > /run/yeodam-b391/b-integration376-bulk.py",
                             "echo '" + digest + "  /run/yeodam-b391/b-integration376-bulk.py' | sha256sum -c -"])
            (EVIDENCE / 'bulk-preparation.json').write_text(json.dumps({'waitSeconds': 45, 'sha256': digest,
                'originalSha256': hashlib.sha256(template.encode()).hexdigest(), 'killClients': False,
                'cutoverDeadlineExtended': False}, indent=2))
        data = base64.b64encode((ROOT / 'scripts/b-integration393-resources.py').read_bytes()).decode()
        m.raw('source', ["printf '%s' '" + data + "' | base64 -d > /run/yeodam-b391/b-integration393-resources.py"])
    else:
        if plan.get('bulkClientWaitSeconds') == 45 and not (EVIDENCE / 'bulk-preparation.json').exists():
            raise RuntimeError('Bounded client wait was not deployed')
        instrument(m, {**plan['rates'][0], 'snapshotBytesPerSecond': plan['snapshotBytesPerSecond']})
        m.execute(plan['rates'][0], 0)


if __name__ == '__main__':
    main()
