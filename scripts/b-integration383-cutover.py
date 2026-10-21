#!/usr/bin/env python3
"""Prearm metadata, freeze the CDC tail, and publish its durable boundary proof."""
import argparse,json,time
from urllib.request import Request,urlopen
from uuid import UUID
from confluent_kafka import Consumer,TopicPartition

BASE='http://127.0.0.1:18083'
CDC_BUDGET_SECONDS=45
class BoundaryError(RuntimeError):
    def __init__(self,code,state):
        super().__init__(code);self.code=code;self.state=state
    def proof(self,run):
        return {'runId':run,'equal':False,'failureCode':self.code,**self.state,'finishedAt':time.time()}
def api(path,method=None):
    with urlopen(Request(BASE+path,method=method),timeout=10) as r:
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
def prepare(c,topic):
    metadata=c.list_topics(topic,timeout=3).topics[topic]
    if metadata.error:raise RuntimeError('Topic metadata unavailable')
    parts=[TopicPartition(topic,p) for p in metadata.partitions]
    # The run's Sink/Group may not exist yet; this is optional coordinator warmup.
    try:c.committed(parts,timeout=3)
    except Exception:pass
    # Actual committed offsets are always checked strictly after the write fence.
    return parts
def ends(c,parts):return {p.partition:c.get_watermark_offsets(p,timeout=3,cached=False)[1] for p in parts}
def caught(c,topic,parts=None,end=None):
    parts=prepare(c,topic) if parts is None else parts
    end=ends(c,parts) if end is None else end
    current={p.partition:p.offset for p in c.committed(parts,timeout=3)}
    return current==end,{'topic':topic,'end':end,'current':current}
def cutover(run,position,marker_timeout=30,*,cutover_at=None,on_proof=None):
    state={'stage':'prepare','events':[],'sourcePosition':position,'groups':[]}
    def mark(stage,**values):
        state['stage']=stage;state.update(values);state['events'].append({'stage':stage,'at':time.time()})
    n='yeodam-b391-'+run[:8];p='staging.yeodam.migration.b391.'+run;a='staging.yeodam.migration.auth368.b391-'+run
    pairs=[('connect-'+n+'-sink',p+'.trips'),('connect-'+n+'-sink-journal',p+'.yeodam_v1_source.migration_trip_request_result'),('yeodam-auth368-b391-'+run,a+'-encrypted')]
    clients=[];audit=None;requests=None;source_offsets={}
    try:
        clients=[consumer(g) for g,_ in pairs];parts=[prepare(c,t) for c,(_,t) in zip(clients,pairs)]
        audit=consumer('yeodam-b391-boundary-'+run)
        request_group='yeodam-b391-'+run;requests=consumer(request_group);request_parts=prepare(requests,p+'.requests')
        mark('marker_wait');marker_deadline=time.monotonic()+marker_timeout
        for topic in [p+'.yeodam_v1_source.migration_cutover_barrier',a+'.source.yeodam_v1_source.migration_cutover_barrier']:
            marker_seen(audit,topic,run,marker_deadline)
        started=time.time();deadline=(cutover_at if cutover_at is not None else started)+CDC_BUDGET_SECONDS
        state['startedAt']=started;state['deadlineAt']=deadline
        # Source BE has gracefully stopped its trip listener before emitting the DB marker.
        # New requests keep accumulating: preserve committed positions, never require request Lag0.
        offsets=requests.committed(request_parts,timeout=3)
        request_offsets={part.partition:part.offset for part in offsets}
        if set(request_offsets)!=set(range(8)) or any(offset<0 for offset in request_offsets.values()):raise BoundaryError('SOURCE_REQUEST_OFFSET_MISSING',state)
        state['requestHandoff']={'group':request_group,'committed':request_offsets,'topic':p+'.requests'}
        mark('source_stop_requested')
        for kind in ('trips','auth-source'):api('/connectors/'+n+'-'+kind+'/stop','PUT')
        for kind in ('trips','auth-source'):
            mark('source_stop_wait',connector=kind)
            while time.time()<deadline:
                status=api('/connectors/'+n+'-'+kind+'/status')
                state['sourceState']={'connector':status['connector']['state'],'tasks':[t['state'] for t in status['tasks']]}
                if status['connector']['state']=='STOPPED' and not status['tasks']:break
                time.sleep(.05)
            else:raise BoundaryError('SOURCE_STOP_DEADLINE',state)
            mark('source_checkpoint',connector=kind)
            offsets=api('/connectors/'+n+'-'+kind+'/offsets').get('offsets',[])
            state['checkpointSummary']={'count':len(offsets),'hasGtids':bool(offsets and offsets[0]['offset'].get('gtids'))}
            if len(offsets)!=1 or not offsets[0]['offset'].get('gtids'):raise BoundaryError('SOURCE_CHECKPOINT_MISSING',state)
            source_offsets[kind]=offsets[0]['offset']
        mark('frozen_tail');tails=[ends(c,ps) for c,ps in zip(clients,parts)]
        while time.time()<deadline:
            values=[caught(c,t,ps,last) for c,(_,t),ps,last in zip(clients,pairs,parts,tails)]
            state['groups']=[v[1] for v in values]
            if all(v[0] for v in values):break
            time.sleep(.05)
        else:raise BoundaryError('FROZEN_TAIL_DEADLINE',state)
        mark('sink_stop')
        for kind in ('sink','sink-journal'):
            api('/connectors/'+n+'-'+kind+'/stop','PUT')
            while time.time()<deadline:
                status=api('/connectors/'+n+'-'+kind+'/status')
                if status['connector']['state']=='STOPPED' and not status['tasks']:break
                time.sleep(.05)
            else:raise BoundaryError('SINK_STOP_DEADLINE',state)
        if time.time()>deadline:raise BoundaryError('PROOF_DEADLINE',state)
        mark('validated')
        proof={'runId':run,'equal':True,'groups':state['groups'],'observedAt':time.time(),'sourceOffsets':source_offsets,'requestHandoff':state['requestHandoff'],'boundaryMethod':'frozen-business-gtids-final-marker-and-graceful-flush','startedAt':started,'finishedAt':time.time(),'elapsedSeconds':time.time()-started,'deadlineAt':deadline,'events':state['events']}
        # Read-only client teardown is outside the critical publication path.
        if on_proof:on_proof(proof)
        return proof
    except BoundaryError:raise
    except Exception as e:
        state['errorClass']=type(e).__name__;raise BoundaryError('BOUNDARY_STAGE_ERROR',state) from None
    finally:
        for c in [requests,audit,*clients]:
            if c is not None:
                try:c.close()
                except Exception:pass # Validation has its own fixed result; cleanup cannot revoke it.
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run-id',required=True);p.add_argument('--source-position',required=True);a=p.parse_args()
    if str(UUID(a.run_id))!=a.run_id:raise ValueError('Canonical Run required')
    print(json.dumps(cutover(a.run_id,a.source_position)),flush=True)
