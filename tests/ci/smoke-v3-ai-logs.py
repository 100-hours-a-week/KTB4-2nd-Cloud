#!/usr/bin/env python3
"""Exact collector image + actual parser/Lua; synthetic local files, no AWS credentials."""
import json,os,re,subprocess,tempfile,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];CHART=ROOT/'deploy/v3/ai-logs'
IMAGE=json.loads((CHART/'image.lock.json').read_text())['image']
def command(*args,timeout=40):return subprocess.run(list(args),capture_output=True,text=True,check=True,timeout=timeout).stdout
name='v3-ai-logs-'+uuid.uuid4().hex[:12]
def line(data,flag='F'):
    return '2026-11-11T00:00:00.000000000Z stdout '+flag+' '+(json.dumps(data) if isinstance(data,dict) else data)+'\n'
with tempfile.TemporaryDirectory(prefix='v3-ai-log-fixture-') as tmp:
    root=Path(tmp);logs=root/'logs';conf=root/'conf';buffer=root/'buffer'
    for p in [logs,conf,buffer]:p.mkdir()
    for p in (CHART/'files').iterdir():(conf/p.name).write_bytes(p.read_bytes())
    cfg=(conf/'fluent-bit.conf').read_text().replace('Read_From_Head Off','Read_From_Head On')
    cfg=cfg.split('[OUTPUT]')[0]+'[OUTPUT]\n    Name stdout\n    Match ai.*\n    Format json_lines\n'
    (conf/'fluent-bit.conf').write_text(cfg)
    api=logs/('ai-api-fixture_ai_ai-'+'a'*64+'.log');runner=logs/('ai-runner-fixture_ai_ai-'+'b'*64+'.log')
    api.write_text(line({'event':'fixture_api','request_id':'fixture-request','job_id':'fixture-job','trace_id':None,'span_id':None,'duration_ms':12,'nested':{'access_token':'fixture-private-value'},'message':'Bearer fixture-private-value https://example.invalid/?token=fixture-private-value'}))
    runner.write_text(line({'event':'fixture_runner','error':{'api_key':'fixture-private-value'},'cookie':'fixture-private-value'}))
    with api.open('a') as f:
        f.write(line('secret startup text fixture-private-value'))
        f.write(line('{"event":"fixture_fragment",','P'));f.write(line('"count":2}','F'))
    (logs/('backend-fixture_trip_ai-'+'c'*64+'.log')).write_text(line({'event':'must_not_collect'}))
    try:
        command('docker','run','-d','--name',name,'--platform','linux/amd64','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','128m','--cpus','0.5','-e','NODE_NAME=fixture-node','-v',str(logs)+':/var/log/containers:ro','-v',str(conf)+':/fluent-bit/etc:ro','-v',str(buffer)+':/buffers','--entrypoint','/fluent-bit/bin/fluent-bit',IMAGE,'-c','/fluent-bit/etc/fluent-bit.conf',timeout=60)
        def events():
            raw=command('docker','logs',name);items=[]
            for text in raw.splitlines():
                try:
                    x=json.loads(text)
                    if isinstance(x,dict) and 'app' in x:items.append(x)
                except json.JSONDecodeError:pass
            return items
        def wait_for(event):
            deadline=time.monotonic()+25
            while time.monotonic()<deadline:
                items=events()
                if any(x['app'].get('event')==event for x in items):return items
                time.sleep(.2)
            diagnostic=subprocess.run(['docker','logs',name],capture_output=True,text=True).stderr
            raise AssertionError('Collector fixture timed out; '+diagnostic[-1600:].replace('fixture-private-value','[fixture]'))
        items=wait_for('fixture_fragment')
        assert {x['app']['event'] for x in items}=={'fixture_api','fixture_runner','collector_unparsed_log','fixture_fragment'}
        a=next(x for x in items if x['app']['event']=='fixture_api')
        assert a['app']['request_id']=='fixture-request' and a['app']['job_id']=='fixture-job' and a['app']['duration_ms']==12
        assert 'trace_id' in a['app'] and a['app']['trace_id'] is None and a['app']['span_id'] is None
        assert a['kubernetes']=={'namespace_name':'ai','pod_name':'ai-api-fixture','container_name':'ai','node_name':'fixture-node'}
        assert 'fixture-private-value' not in json.dumps(items)
        command('docker','restart',name)
        with api.open('a') as f:f.write(line({'event':'fixture_after_restart'}))
        items=wait_for('fixture_after_restart')
        assert sum(x['app']['event']=='fixture_api' for x in items)==1,'Persisted offsets must avoid restart replay'
        assert len(items)==5
        print(json.dumps({'passed':True,'checks':['both AI roles','JSON identifiers/null/number preservation','nested credentials and free-text masking','unparsed omission','CRI fragment reconstruction','non-AI exclusion','restart offset retention'],'events':len(items),'awsAccess':False}))
    finally:
        subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=15)
