#!/usr/bin/env python3
"""Run scoped #376 host actions and retain sanitized SSM evidence locally."""
import argparse
import base64
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
HOSTS = {'source':'i-05d320e3a87200d5a','target':'i-05535ae6ff812972c',
         'kafka':'i-0b86e517fda2cfafc','generator':'i-0c53b65eb35dd2b42'}
EVIDENCE = ROOT.parent/'KTB4-2nd-Cloud/.local-docs/evidence/383-b-integration'

def aws(*args):
    p=subprocess.run(['aws',*args,'--profile','yeodam-admin','--region','ap-northeast-2','--output','json'],capture_output=True,text=True)
    if p.returncode: raise RuntimeError('AWS call failed: '+p.stderr[:200])
    return json.loads(p.stdout) if p.stdout.strip() else {}

def send(host,commands,timeout=1800):
    request={'InstanceIds':[HOSTS[host]],'DocumentName':'AWS-RunShellScript',
             'Comment':'Cloud #383 B integration '+host,
             'Parameters':{'commands':['set -eu','umask 077',*commands],'executionTimeout':[str(timeout)]}}
    with tempfile.NamedTemporaryFile('w',suffix='.json') as f:
        json.dump(request,f);f.flush()
        cid=aws('ssm','send-command','--cli-input-json','file://'+f.name)['Command']['CommandId']
    EVIDENCE.mkdir(parents=True,exist_ok=True)
    (EVIDENCE/(cid+'.request.json')).write_text(json.dumps({'host':host,'commandId':cid,'submittedAt':time.time()}))
    return cid

def result(host,cid):
    r=aws('ssm','get-command-invocation','--instance-id',HOSTS[host],'--command-id',cid)
    # Host scripts must never print credentials, payloads or environment contents.
    value={k:r.get(k) for k in ('CommandId','Status','ResponseCode','ExecutionStartDateTime','ExecutionEndDateTime','StandardOutputContent','StandardErrorContent')}
    (EVIDENCE/(cid+'.result.json')).write_text(json.dumps(value,indent=2))
    return value

def deploy(source,target):
    data=base64.b64encode((ROOT/source).read_bytes()).decode()
    return ['install -d -m 0755 '+shlex.quote(str(Path(target).parent)),
            "printf '%s' '"+data+"' | base64 -d > "+shlex.quote(target),
            'chmod 0700 '+shlex.quote(target)]

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('host',choices=HOSTS);p.add_argument('action',choices=('inventory','run','result'))
    p.add_argument('--script');p.add_argument('--args',nargs=argparse.REMAINDER,default=[]);p.add_argument('--command-id');p.add_argument('--wait',type=int,default=0)
    a=p.parse_args()
    if a.action=='result': cid=a.command_id
    else:
        if aws('sts','get-caller-identity')['Account']!='483175530259': raise RuntimeError('Wrong AWS account')
        commands=deploy('scripts/b-integration376-host.py','/run/yeodam-b376/host.py')
        if a.action=='run':
            if a.script: commands+=deploy(a.script,'/run/yeodam-b376/'+Path(a.script).name)
            remote='/run/yeodam-b376/'+Path(a.script).name if a.script else '/run/yeodam-b376/host.py'
            commands+=['python3 '+remote+' '+shlex.join(a.args)]
        else: commands+=['python3 /run/yeodam-b376/host.py inventory']
        cid=send(a.host,commands)
        print(json.dumps({'host':a.host,'commandId':cid}),flush=True)
    end=time.monotonic()+a.wait
    while True:
        try:r=result(a.host,cid)
        except RuntimeError:
            if time.monotonic()>=end: raise
            time.sleep(2);continue
        if r['Status'] not in ('Pending','InProgress','Delayed') or time.monotonic()>=end:
            print(json.dumps(r,ensure_ascii=False));return
        time.sleep(2)

if __name__=='__main__':main()
