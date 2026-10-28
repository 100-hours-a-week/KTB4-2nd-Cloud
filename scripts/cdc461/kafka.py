#!/usr/bin/env python3
"""Read-only observers and scoped Sink lifecycle on the existing owned Worker."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.request import Request, urlopen
from uuid import UUID

ROOT=Path('/var/lib/yeodam/cdc461')
WORKER='yeodam-b383-connect'
BASE='http://127.0.0.1:18083'


def api(path, method=None, data=None, port=18083):
    request=Request('http://127.0.0.1:'+str(port)+path, method=method,
                    data=None if data is None else json.dumps(data).encode(), headers={'Content-Type':'application/json'})
    with urlopen(request,timeout=5) as result:
        content=result.read();return json.loads(content) if content else {}


def worker():
    v=json.loads(subprocess.check_output(['docker','inspect',WORKER],text=True))[0]
    if v['Config'].get('Labels',{}).get('yeodam.issue')!='383' or v['HostConfig']['NanoCpus']!=1000000000 or v['HostConfig']['Memory']!=939524096:
        raise RuntimeError('Owned Worker identity/resource limits differ')
    return v


def parent_topics(c):
    prefix='staging.yeodam.migration.b391.'+c['parentRun']
    return [(prefix+'.trips',c['tripEnd']),
            (prefix+'.yeodam_v1_source.migration_trip_request_result',c['journalEnd'])]


def consumers(c, prefix):
    from confluent_kafka import Consumer,TopicPartition
    result=[]
    for suffix,(topic,end) in zip(('sink','sink-journal'),parent_topics(c)):
        client=Consumer({'bootstrap.servers':'10.30.10.95:9092','group.id':'connect-'+prefix+'-'+suffix,
                         'enable.auto.commit':False,'enable.auto.offset.store':False,'socket.timeout.ms':5000})
        metadata=client.list_topics(topic,timeout=5).topics[topic]
        if metadata.error or set(metadata.partitions)!={int(k) for k in end}: raise RuntimeError('Preserved Topic/partitions missing')
        parts=[TopicPartition(topic,int(k)) for k in end]
        result.append((client,topic,parts,end))
    return result


def offsets(items, fresh=False):
    rows=[]
    for client,topic,parts,end in items:
        begin={};actual_end={}
        for part in parts:
            low,high=client.get_watermark_offsets(part,timeout=3,cached=False)
            begin[str(part.partition)]=low;actual_end[str(part.partition)]=high
        current={str(p.partition):p.offset for p in client.committed(parts,timeout=3)}
        if any(begin.values()) or actual_end!=end: raise RuntimeError('Preserved Topic range differs; never replay a truncated stream')
        if fresh and any(value>=0 for value in current.values()): raise RuntimeError('Diagnostic Group has committed offsets already')
        rows.append({'topic':topic,'begin':begin,'end':end,'committed':current,'equal':current==end,
                     'lag':sum(end[k]-max(0,current[k]) for k in end)})
    return rows


def host_cgroups():
    out={}
    for name in [WORKER,'yeodam-v2-staging-kafka-kafka-1']:
        v=json.loads(subprocess.check_output(['docker','inspect',name],text=True))[0];pid=v['State']['Pid']
        if not pid:continue
        root=Path('/proc')/str(pid)/'root/sys/fs/cgroup'
        values={}
        for file in ['cpu.stat','memory.current','memory.events']:
            try:values[file]=(root/file).read_text().strip()
            except OSError:values[file]=None
        out[name]=values
    return out


def check(directory,c,prefix):
    initial=worker();directory.mkdir(parents=True,exist_ok=False,mode=0o700)
    (directory/'initial-worker.json').write_text(json.dumps({'running':initial['State']['Running']}))
    shared=api('/connectors/yeodam-staging-outbox/status',port=8083)
    if shared['connector']['state']!='RUNNING' or any(t['state']!='RUNNING' for t in shared['tasks']):raise RuntimeError('Shared Outbox unhealthy')
    if not initial['State']['Running']:subprocess.run(['docker','start',WORKER],check=True,stdout=subprocess.DEVNULL)
    end=time.monotonic()+180
    while time.monotonic()<end:
        try:plugins=api('/connector-plugins');break
        except Exception:time.sleep(2)
    else:raise RuntimeError('Owned Worker not ready')
    if not any(p['class']=='io.debezium.connector.jdbc.JdbcSinkConnector' and p['version']=='3.4.3.Final' for p in plugins):
        raise RuntimeError('Expected reviewed JDBC version')
    # Plugin discovery becomes available before the status/config cache finishes
    # recovering. Require two stable observations, without stopping old connectors.
    stable=0;ready_end=time.monotonic()+60
    with (directory/'worker-readiness.jsonl').open('w') as file:
        while time.monotonic()<ready_end:
            states=api('/connectors?expand=status')
            clean=all(v['status']['connector']['state']=='STOPPED' and not v['status']['tasks'] for v in states.values())
            stable=stable+1 if clean else 0
            file.write(json.dumps({'at':time.time(),'connectors':len(states),'notStoppedCount':sum(v['status']['connector']['state']!='STOPPED' or bool(v['status']['tasks']) for v in states.values()),'stable':stable})+'\n');file.flush()
            if stable>=2:break
            time.sleep(1)
        else:raise RuntimeError('Previous Connector state did not settle; no Target replacement')
    items=consumers(c,prefix)
    try:values=offsets(items,True)
    finally:
        for client,*_ in items:client.close()
    print(json.dumps({'ready':True,'allOldConnectorsStopped':len(states),'ranges':values,'workerInitiallyRunning':initial['State']['Running']}),flush=True)


def jmx(directory,prefix,seconds):
    subprocess.run(['docker','cp',str(ROOT/'Metrics461.class'),WORKER+':/tmp/Metrics461.class'],check=True,capture_output=True)
    # Run under the image's existing user/PID context for the local-only attach
    # protocol. Observer CPU and read wall time are reported, not hidden; no
    # cgroup limit is increased. This is a diagnostic, not a production benchmark.
    if not worker()['State']['Running']:raise RuntimeError('Worker is not running')
    with (directory/'jmx.jsonl').open('w') as out,(directory/'jmx.stderr.private').open('w') as err:
        cmd=['docker','exec',WORKER,'java','--add-modules','jdk.attach','-Xms8m','-Xmx32m','-XX:MaxMetaspaceSize=32m',
             '-XX:CompressedClassSpaceSize=8m','-XX:ReservedCodeCacheSize=16m','-XX:ActiveProcessorCount=1',
             '-Dsun.rmi.transport.tcp.responseTimeout=3000','-cp','/tmp','Metrics461',prefix,str(seconds),'2']
        result=subprocess.run(cmd,stdout=out,stderr=err)
    if result.returncode:raise RuntimeError('JMX observer failed; private stderr retained')
    print(json.dumps({'observerCompleted':True,'observerInWorkerCgroup':True}),flush=True)


def sample(directory,c,prefix):
    items=consumers(c,prefix);end=time.monotonic()+c['diagnosticDeadlineSeconds']+90
    try:
        with (directory/'offsets.jsonl').open('w') as file:
            while time.monotonic()<end and not (directory/'STOP').exists():
                began=time.monotonic();rows=offsets(items)
                value={'at':time.time(),'groups':rows,'cgroups':host_cgroups(),'sampleMillis':(time.monotonic()-began)*1000}
                file.write(json.dumps(value)+'\n');file.flush()
                if all(r['equal'] for r in rows):(directory/'caught.json').write_text(json.dumps(value))
                time.sleep(max(0,c['sampleIntervalSeconds']-(time.monotonic()-began)))
    finally:
        for client,*_ in items:client.close()


def register(directory,c,prefix):
    # Only the two Sink configs are derived from the validated immutable bundle.
    configs=json.loads((directory/'sink-configs.private.json').read_text())
    for suffix,config in zip(('sink','sink-journal'),configs):
        name=prefix+'-'+suffix
        if config['name']!=name or name in api('/connectors'):raise RuntimeError('Diagnostic connector already exists')
        if config['connection.url']!='jdbc:mysql://10.30.10.45:3306/yeodam_v1_source?sslMode=REQUIRED' or config['errors.tolerance']!='none':raise RuntimeError('Unexpected destination/error policy')
        expected=(8,1,1,2) if suffix=='sink' else (1,100,100,1)
        actual=tuple(int(config[k]) for k in ['tasks.max','batch.size','consumer.override.max.poll.records','connection.pool.max_size'])
        if actual!=expected:raise RuntimeError('Frozen Sink configuration differs')
        api('/connectors','POST',{'name':name,'config':config})
    print(json.dumps({'registeredAt':time.time(),'connectors':[x['name'] for x in configs]}),flush=True)


def stop(directory,prefix):
    (directory/'STOP').touch()
    subprocess.run(['docker','exec',WORKER,'touch','/tmp/stop-'+prefix],check=True,capture_output=True)
    names=set(api('/connectors'))
    for suffix in ('sink','sink-journal'):
        name=prefix+'-'+suffix
        if name in names:api('/connectors/'+name+'/stop','PUT')
    end=time.monotonic()+60
    while time.monotonic()<end:
        states=[api('/connectors/'+prefix+'-'+s+'/status') for s in ('sink','sink-journal') if prefix+'-'+s in names]
        if all(v['connector']['state']=='STOPPED' and not v['tasks'] for v in states):break
        time.sleep(.5)
    else:raise RuntimeError('Diagnostic Sink cleanup pending; keep Target protected')
    print(json.dumps({'stopped':True,'offsetsDeleted':False}),flush=True)


def close(directory):
    initial=json.loads((directory/'initial-worker.json').read_text())
    statuses=api('/connectors?expand=status')
    if any(v['status']['connector']['state']!='STOPPED' or v['status']['tasks'] for v in statuses.values()):raise RuntimeError('Active tasks remain; do not stop Worker')
    if not initial['running']:subprocess.run(['docker','stop',WORKER],check=True,capture_output=True)
    shared=api('/connectors/yeodam-staging-outbox/status',port=8083)
    print(json.dumps({'restoredWorkerRunning':initial['running'],'outbox':shared['connector']['state'],'outboxTasks':[t['state'] for t in shared['tasks']],'topicsOffsetsDeleted':False}),flush=True)


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['check','warmup','jmx','sample','register','stop','close','progress']);p.add_argument('--run',required=True);p.add_argument('--contract',required=True);a=p.parse_args()
    if str(UUID(a.run))!=a.run:raise ValueError('Canonical UUID required')
    if '10.30.10.95' not in subprocess.check_output(['hostname','-I'],text=True).split():raise RuntimeError('Wrong Kafka host')
    os.umask(0o077);c=json.loads(Path(a.contract).read_text());directory=ROOT/a.run;prefix='yeodam-diag461-'+a.run[:8]
    if a.action=='check':check(directory,c,prefix)
    elif a.action in ('warmup','jmx'):jmx(directory,prefix,8 if a.action=='warmup' else c['diagnosticDeadlineSeconds']+90)
    elif a.action=='sample':sample(directory,c,prefix)
    elif a.action=='register':register(directory,c,prefix)
    elif a.action=='stop':stop(directory,prefix)
    elif a.action=='close':close(directory)
    else:
        file=directory/'offsets.jsonl';last=json.loads(file.read_text().splitlines()[-1]) if file.exists() and file.stat().st_size else None
        statuses=api('/connectors?expand=status')
        owned={k:{'connector':v['status']['connector']['state'],'tasks':[{ 'id':t['id'],'state':t['state']} for t in v['status']['tasks']]} for k,v in statuses.items() if k.startswith(prefix+'-sink')}
        print(json.dumps({'last':last,'statuses':owned}),flush=True)


if __name__=='__main__':main()
