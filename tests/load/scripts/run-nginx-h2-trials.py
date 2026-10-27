#!/usr/bin/env python3
"""Run #442 real HTTP/2 POST stages with unchanged defenses and independent external guards."""
import argparse
import importlib.util
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import subprocess
import time
from uuid import UUID, uuid4

APP = 'i-05d320e3a87200d5a'
GENERATOR = 'i-0c53b65eb35dd2b42'
OPS = '/opt/yeodam/ops/nginx-edge438'
ORIGIN = 'https://v1-staging.yeodam-2gether.com'
LOAD_OPS = '/opt/yeodam-load/nginx-h2-442'
STAGES = [('smoke1', 1, 1), ('small16', 64, 30), ('medium64', 256, 30),
          ('instructor141', 0, 299), ('instructor128', 0, 299)]
spec = importlib.util.spec_from_file_location('browser_guard447', Path(__file__).with_name('nginx-browser-guard.py'))
browser_guard = importlib.util.module_from_spec(spec);spec.loader.exec_module(browser_guard)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--context', type=Path, required=True)
    p.add_argument('--external', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--browser', type=Path, help='Require fresh, baseline-ready browser checkpoint and no sticky failure')
    p.add_argument('--stages', nargs='+', choices=[x[0] for x in STAGES], default=[x[0] for x in STAGES])
    p.add_argument('--execute', action='store_true')
    a = p.parse_args()
    c = json.loads(a.context.read_text())
    UUID(c['captureId']);UUID(c['normalId'])
    if c['app'] != APP or c['generator'] != GENERATOR or c['ops'] != OPS:
        p.error('Exact existing staging resources only')
    if not a.execute:
        print(json.dumps({'execute': False, 'stages': STAGES}));return
    a.output.mkdir(parents=True, exist_ok=False)
    aws = ['aws', '--profile', 'yeodam-admin', '--region', 'ap-northeast-2']
    def call(args):
        return json.loads(subprocess.check_output(aws+args+['--output', 'json'], text=True, timeout=20))
    if call(['sts', 'get-caller-identity'])['Account'] != '483175530259':
        raise ValueError('Unexpected AWS account')
    def send(instance, command):
        return call(['ssm', 'send-command', '--instance-ids', instance, '--document-name', 'AWS-RunShellScript',
            '--comment', 'Cloud442 HTTP2 POST replay', '--parameters',
            json.dumps({'commands':['set -eu\n'+command], 'executionTimeout':['450']})])['Command']['CommandId']
    def result(instance, cid):
        try:
            return call(['ssm', 'get-command-invocation', '--instance-id', instance, '--command-id', cid])
        except subprocess.CalledProcessError:
            return {'Status':'Pending'}
    def wait(instance, cid, unit=None):
        deadline = time.monotonic()+460
        stopped = False
        while time.monotonic() < deadline:
            if unit and not external_ok() and not stopped:
                send(GENERATOR, shlex.join(['systemctl', 'stop', unit]))
                stopped = True
            r = result(instance, cid)
            if r['Status'] not in ['Pending','InProgress','Delayed']:
                return r, stopped
            time.sleep(2)
        if unit:
            send(GENERATOR, shlex.join(['systemctl','stop',unit]))
        raise TimeoutError('Stage did not finish; stop requested')
    def external_ok():
        try:
            if a.browser and not browser_guard.healthy(a.browser, c['captureId']):
                return False
            x = json.loads((a.external/'checkpoint.json').read_text())
            age = (datetime.now(timezone.utc)-datetime.fromisoformat(x['updatedAt'])).total_seconds()
            return age < 10 and x['errors'] == 0 and x['failures'] == 0 and not x['guardTriggered']
        except (OSError, ValueError, KeyError):
            return False
    def host_guard(label):
        command = "python3 - <<'HOST'\n"+f'''
from pathlib import Path
from datetime import datetime,timezone
from collections import deque
import json,hashlib
p=Path('/var/lib/yeodam/nginx-rehearsal/{c['captureId']}')
cap=json.loads((p/'checkpoint.json').read_text());ena=json.loads((p/'ena/checkpoint.json').read_text())
normal=json.loads(Path('/var/lib/yeodam/nginx-connections-426/{c['normalId']}/checkpoint.json').read_text())
recent=[json.loads(x) for x in deque((p/'samples.jsonl').open(),maxlen=5)]
age=lambda s:(datetime.now(timezone.utc)-datetime.fromisoformat(s)).total_seconds()
ok=age(cap['updatedAt'])<5 and age(ena['updatedAt'])<5 and age(normal['updatedAt'])<10
ok=ok and cap['sampleErrors']==0 and ena['errors']==0 and normal['failureCount']==0
ok=ok and not any(r.get('memoryAvailableKiB',0)<524288 or r.get('diskPercent',100)>=80 for r in recent)
ok=ok and not all(r.get('cpuPercent',100)>95 for r in recent)
ok=ok and hashlib.sha256(Path('/etc/nginx/sites-enabled/yeodam-v1-staging').read_bytes()).hexdigest()=={c['siteSha']!r}
ok=ok and hashlib.sha256(Path('/etc/nginx/nginx.conf').read_bytes()).hexdigest()=={c['mainSha']!r}
print(json.dumps({{'ok':ok,'capture':cap,'ena':ena,'normal':normal}}))
'''+'\nHOST'
        cid = send(APP, command);r,_ = wait(APP,cid)
        (a.output/(label+'-host.json')).write_text(json.dumps(r,indent=2))
        if r['Status'] != 'Success':raise RuntimeError('Host guard unavailable')
        state=json.loads(r['StandardOutputContent'])
        if not state['ok']:raise RuntimeError('Host resource/capture guard failed')
        if state['ena'].get('counterDelta') is None:
            raise RuntimeError('ENA counter baseline reset/missing; no escalation')
        if any(state['ena']['counterDelta'].values()):
            raise RuntimeError('New ENA shaping observed; preserve first failure and stop escalation')
        return state
    trials=[]
    try:
        for name, rate, seconds in STAGES:
            if name not in a.stages:
                continue
            if not external_ok():raise RuntimeError('External probe failed or stale; no more load')
            host_guard(name+'-before')
            unit='yeodam-h2-442-'+c['captureId'][:8]+'-'+name
            folder='/var/lib/yeodam/nginx-h2-442/'+c['captureId']+'/'+name
            stage_id=str(uuid4())
            args=['python3',LOAD_OPS+'/scripts/run-nginx-h2-stage.py','--stage',name,
                '--capture-id',c['captureId'],'--run-id',stage_id,'--confirm-target',ORIGIN]
            inner=shlex.join(args)
            command=shlex.join(['systemd-run','--quiet','--wait','--collect',
                '--unit='+unit,'--property=RuntimeMaxSec='+str(seconds+45),'--property=MemoryMax=768M',
                '--property=CPUQuota=150%','/bin/bash','-c',inner])
            began=datetime.now(timezone.utc).isoformat();cid=send(GENERATOR,command)
            (a.output/'active-stage.json').write_text(json.dumps({'stage':name,'startedAt':began,'unit':unit}))
            print(json.dumps({'stage':name,'startedAt':began,'commandId':cid}),flush=True)
            r, stopped = wait(GENERATOR,cid,unit)
            (a.output/(name+'-command.json')).write_text(json.dumps(r,indent=2))
            trials.append({'stage':name,'startedAt':began,'endedAt':datetime.now(timezone.utc).isoformat(),
                           'stageRunId':stage_id,'commandId':cid,'status':r['Status'],'guardStopped':stopped,'remoteFolder':folder})
            (a.output/'stages.json').write_text(json.dumps(trials,indent=2))
            host_guard(name+'-after')
            print(json.dumps({'stage':name,'status':r['Status'],'externalHealthy':external_ok()}),flush=True)
            summary_id=send(GENERATOR, 'tail -c 10000 '+shlex.quote(folder+'/run.log'))
            summary,_=wait(GENERATOR,summary_id)
            (a.output/(name+'-summary.json')).write_text(json.dumps(summary,indent=2))
            print(summary['StandardOutputContent'],flush=True)
            if stopped or not external_ok():raise RuntimeError('Normal external probe guard stopped load')
            if r['Status'] != 'Success':raise RuntimeError('Workload failed; inspect before escalation')
            time.sleep(5)
    except BaseException:
        if 'unit' in locals():
            send(GENERATOR, shlex.join(['systemctl','stop',unit]))
        raise
    finally:
        (a.output/'stages.json').write_text(json.dumps(trials,indent=2))
        (a.output/'finished.json').write_text(json.dumps({'endedAt':datetime.now(timezone.utc).isoformat(),
            'browserGuardEnabled':bool(a.browser), 'scope':'Controller finished; inspect each workload and browser result separately'}))


if __name__ == '__main__':
    main()
