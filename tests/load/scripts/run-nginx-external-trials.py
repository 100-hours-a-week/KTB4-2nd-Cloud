#!/usr/bin/env python3
"""Run #438 bounded stages only after detached host/ENA and independent external probes are fresh."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import subprocess
import time
from uuid import UUID

APP = 'i-05d320e3a87200d5a'
GENERATOR = 'i-0c53b65eb35dd2b42'
OPS = '/opt/yeodam/ops/nginx-edge438'
ORIGIN = 'https://v1-staging.yeodam-2gether.com'
STAGES = [('http100', 100, None), ('http450', 450, None),
          ('hold192', 0, 'hold'), ('churn120', 0, 'churn'), ('mixed450', 450, 'churn')]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--context', type=Path, required=True)
    p.add_argument('--external', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
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
            '--comment', 'Cloud438 bounded observation/replay', '--parameters',
            json.dumps({'commands':['set -eu\n'+command], 'executionTimeout':['90']})])['Command']['CommandId']
    def result(instance, cid):
        try:
            return call(['ssm', 'get-command-invocation', '--instance-id', instance, '--command-id', cid])
        except subprocess.CalledProcessError:
            return {'Status':'Pending'}
    def wait(instance, cid, unit=None):
        deadline = time.monotonic()+100
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
        for name, rate, mode in STAGES:
            if name not in a.stages:
                continue
            if not external_ok():raise RuntimeError('External probe failed or stale; no more load')
            host_guard(name+'-before')
            unit='yeodam-edge438-'+c['captureId'][:8]+'-'+name
            folder='/var/lib/yeodam/nginx-edge438/'+c['captureId']+'/'+name
            tasks=[]
            if rate:
                tasks.append(shlex.join(['env','K6_EDGE_EXECUTE=1','K6_EDGE_CONFIRM_TARGET='+ORIGIN,
                    'K6_EDGE_RPS='+str(rate),'K6_EDGE_SECONDS=30','GODEBUG=http2client=0','k6','run',
                    '--summary-export',folder+'/http-summary.json',OPS+'/nginx-edge-replay.js'])+
                    ' > '+shlex.quote(folder+'/http.log')+' 2>&1 & hp=$!')
            if mode:
                tasks.append(shlex.join(['python3',OPS+'/nginx-tcp-capacity.py','--mode',mode,
                    '--connections','192','--rate','20' if mode=='hold' else '120','--seconds','30',
                    '--confirm-target',ORIGIN,'--output',folder+'/tcp.json','--execute'])+
                    ' > '+shlex.quote(folder+'/tcp.log')+' 2>&1 & tp=$!')
            # Always wait both children; a failed workload does not orphan the other.
            tasks.append('code=0')
            if rate:tasks.append('wait "$hp" || code=$?')
            if mode:tasks.append('wait "$tp" || code=$?')
            tasks.append('exit "$code"')
            inner='\n'.join(tasks)
            command='mkdir -p '+shlex.quote(folder)+'\n'+shlex.join(['systemd-run','--quiet','--wait','--collect',
                '--unit='+unit,'--property=RuntimeMaxSec=45','--property=MemoryMax=512M',
                '--property=CPUQuota=150%','/bin/bash','-c',inner])
            began=datetime.now(timezone.utc).isoformat();cid=send(GENERATOR,command)
            print(json.dumps({'stage':name,'startedAt':began,'commandId':cid}),flush=True)
            r, stopped = wait(GENERATOR,cid,unit)
            (a.output/(name+'-command.json')).write_text(json.dumps(r,indent=2))
            trials.append({'stage':name,'startedAt':began,'endedAt':datetime.now(timezone.utc).isoformat(),
                           'commandId':cid,'status':r['Status'],'guardStopped':stopped,'remoteFolder':folder})
            (a.output/'stages.json').write_text(json.dumps(trials,indent=2))
            host_guard(name+'-after')
            print(json.dumps({'stage':name,'status':r['Status'],'externalHealthy':external_ok()}),flush=True)
            if stopped or not external_ok():raise RuntimeError('Normal external probe guard stopped load')
            if r['Status'] != 'Success':raise RuntimeError('Workload failed; inspect before escalation')
            time.sleep(5)
    finally:
        (a.output/'stages.json').write_text(json.dumps(trials,indent=2))


if __name__ == '__main__':
    main()
