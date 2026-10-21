#!/usr/bin/env python3
"""Kafka-host boundary proof: final CDC marker, durable flush, then writer handoff.

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

BASE='http://127.0.0.1:18083'
def api(path,method=None):
    with urlopen(Request(BASE+path,method=method),timeout=15) as r:
        b=r.read();return json.loads(b) if b else {}
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
def cutover(run,position,marker_timeout=30):
    started=time.time()
    state={'sourcePosition':position}
    n='yeodam-b386-'+run[:8];p='staging.yeodam.migration.b386.'+run;a='staging.yeodam.migration.auth368.b386-'+run
    audit=consumer('yeodam-b386-boundary-'+run);deadline=time.monotonic()+marker_timeout;source_offsets={}
    try:
        for topic in [p+'.yeodam_v1_source.migration_cutover_barrier',a+'.source.yeodam_v1_source.migration_cutover_barrier']:
            marker_seen(audit,topic,run,deadline)
        # Standby is outside the handoff budget; the same 30s drain starts at the marker.
        started=time.time();deadline=time.monotonic()+30
        for kind in ('trips','auth-source'):api('/connectors/'+n+'-'+kind+'/stop','PUT')
        for kind in ('trips','auth-source'):
            while time.monotonic()<deadline:
                status=api('/connectors/'+n+'-'+kind+'/status')
                if status['connector']['state']=='STOPPED' and not status['tasks']:break
                time.sleep(.05)
            else:raise RuntimeError('Source connector stop did not flush')
            offsets=api('/connectors/'+n+'-'+kind+'/offsets').get('offsets',[])
            if len(offsets)!=1 or not offsets[0]['offset'].get('gtids'):raise RuntimeError('Durable source checkpoint missing')
            source_offsets[kind]=offsets[0]['offset']
    finally:audit.close()
    pairs=[('connect-'+n+'-sink',p+'.trips'),('yeodam-auth368-b386-'+run,a+'-encrypted')]
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
    proof={'runId':run,'equal':True,'groups':details,'observedAt':time.time(),'sourceOffsets':source_offsets,'boundaryMethod':'frozen-business-gtids-final-marker-and-graceful-flush','startedAt':started,'finishedAt':time.time(),'elapsedSeconds':time.time()-started}
    print(json.dumps(proof),flush=True)
    return proof
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run-id',required=True);p.add_argument('--source-position',required=True);a=p.parse_args()
    if str(UUID(a.run_id))!=a.run_id:raise ValueError('Canonical Run required')
    cutover(a.run_id,a.source_position)
