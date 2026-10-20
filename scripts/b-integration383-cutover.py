#!/usr/bin/env python3
"""Owned Source-host cutover: final CDC marker, durable flush, then writer handoff.

No Kafka offset reset or shared worker configuration change. The marker is written
by root only after read_only has blocked application writes. Seeing it allows one
planned connector stop to flush offsets without waiting for the worker's periodic
checkpoint. Every source coordinate and downstream committed offset is still checked.
"""
import argparse,json,subprocess,time
from pathlib import Path
from urllib.request import Request,urlopen
from uuid import UUID
from confluent_kafka import Consumer,TopicPartition

BASE='http://10.30.10.95:8083'
def api(path,method=None):
    with urlopen(Request(BASE+path,method=method),timeout=15) as r:
        b=r.read();return json.loads(b) if b else {}
def control(run,action):
    subprocess.run(['python3','/run/yeodam-b383/b-integration376-control.py',action,'--run-id',run],check=True)
def position_at_least(actual,expected):
    f,p=actual.get('file',''),actual.get('pos',0);ef,ep=expected.split('\t')[:2]
    prefix,_,num=f.rpartition('.');wanted,_,n=ef.rpartition('.')
    return prefix==wanted and num.isdigit() and n.isdigit() and (int(num),int(p))>=(int(n),int(ep))
def consumer(group):
    return Consumer({'bootstrap.servers':'10.30.10.95:9092','group.id':group,'enable.auto.commit':False,'enable.auto.offset.store':False,'socket.timeout.ms':5000})
def marker_seen(c,topic,run,deadline):
    c.assign([TopicPartition(topic,0,0)])
    while time.monotonic()<deadline:
        msg=c.poll(.1)
        if msg is not None:
            if msg.error():raise RuntimeError('CDC marker stream error')
            if run.encode() in (msg.value() or b''):return
    raise RuntimeError('Final CDC marker not received')
def caught(c,topic):
    metadata=c.list_topics(topic,timeout=3).topics[topic]
    parts=[TopicPartition(topic,p) for p in metadata.partitions]
    end={p.partition:c.get_watermark_offsets(p,timeout=3,cached=False)[1] for p in parts}
    current={p.partition:p.offset for p in c.committed(parts,timeout=3)}
    return current==end,{'topic':topic,'end':end,'current':current}
def cutover(run,at):
    root=Path('/run/yeodam-b383')/run
    while time.time()<at:time.sleep(min(.2,at-time.time()))
    if not (root/'cutover-ready').exists():raise RuntimeError('Cutover preparation incomplete at the fixed deadline')
    for origin in ('source','target'):
        if not (root/(origin+'.projection-ready')).exists():raise RuntimeError('Authentication projection not prepared')
        unit='yeodam-b383-auth-'+('target-' if origin=='target' else '')+run[:8]+'.service'
        subprocess.run(['systemctl','is-active','--quiet',unit],check=True)
    started=time.time();control(run,'fence')
    state=json.loads((Path('/var/lib/yeodam/b383')/(run+'.json')).read_text())
    n='yeodam-b383-'+run[:8];p='staging.yeodam.migration.b383.'+run;a='staging.yeodam.migration.auth368.b383-'+run
    audit=consumer('yeodam-b383-boundary-'+run);deadline=time.monotonic()+30
    try:
        for topic in [p+'.yeodam_v1_source.migration_cutover_barrier',a+'.source.yeodam_v1_source.migration_cutover_barrier']:
            marker_seen(audit,topic,run,deadline)
        for kind in ('trips','auth-source'):api('/connectors/'+n+'-'+kind+'/stop','PUT')
        for kind in ('trips','auth-source'):
            while time.monotonic()<deadline:
                status=api('/connectors/'+n+'-'+kind+'/status')
                if status['connector']['state']=='STOPPED' and not status['tasks']:break
                time.sleep(.05)
            else:raise RuntimeError('Source connector stop did not flush')
            offsets=api('/connectors/'+n+'-'+kind+'/offsets').get('offsets',[])
            if len(offsets)!=1 or not position_at_least(offsets[0]['offset'],state['sourcePosition']):raise RuntimeError('Flushed source position is behind fence')
    finally:audit.close()
    pairs=[('connect-'+n+'-sink',p+'.trips'),('yeodam-auth368-b383-'+run,a+'-encrypted')]
    clients=[consumer(g) for g,_ in pairs];details=[]
    try:
        while time.monotonic()<deadline:
            values=[caught(c,t) for c,(_,t) in zip(clients,pairs)]
            if all(v[0] for v in values):details=[v[1] for v in values];break
            time.sleep(.05)
        else:raise RuntimeError('Frozen CDC tail remains unapplied')
    finally:
        for c in clients:c.close()
    api('/connectors/'+n+'-sink/stop','PUT')
    proof={'runId':run,'equal':True,'groups':details,'observedAt':time.time(),'boundaryMethod':'final-binlog-marker-and-graceful-flush'}
    (Path('/var/lib/yeodam/b383')/(run+'.cdc-drained.json')).write_text(json.dumps(proof))
    control(run,'auth-boundary');control(run,'promote')
    subprocess.run(['python3','/run/yeodam-b383/b-integration376-app.py','enable-consumer','--run-id',run],check=True)
    print(json.dumps({'phase':'cutover_complete','runId':run,'startedAt':started,'finishedAt':time.time(),'elapsedSeconds':time.time()-started}),flush=True)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run-id',required=True);p.add_argument('--at',type=float,required=True);a=p.parse_args()
    if str(UUID(a.run_id))!=a.run_id:raise ValueError('Canonical Run required')
    cutover(a.run_id,a.at)
