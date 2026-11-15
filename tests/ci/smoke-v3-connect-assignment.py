#!/usr/bin/env python3
"""Real active MySQL Connector; local Kafka-only network loss. Synthetic data only."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from v3_connect_recovery import parse_events


def now():return datetime.now(timezone.utc).isoformat()


def exercise(source,c):
    docker=c['docker'];worker=c['worker'];net=c['net'];out=c['output']
    result={'scope':'synthetic active MySQL Connector; Kafka-only Docker network disconnect, not Kubernetes Chaos Mesh',
            'scheduledRebalanceMaxDelayMs':0,'faultSeconds':90,'cpuLimit':.5,'memoryMi':768,
            'productionRtoProven':False,'offsetsReset':False,'manualTaskRestart':False}
    def persist():(out/'assignment-recovery-result.json').write_text(json.dumps(result,indent=2)+'\n')
    def state():return json.loads(docker('inspect',worker).stdout)[0]
    def reachable(host,port):
        return docker('exec',worker,'timeout','2','bash','-c',
                      'exec 3<>/dev/tcp/"$1"/"$2"','check',host,str(port),check=False).returncode==0
    def status():
        response=docker('exec',worker,'curl','-fsS','--max-time','3',
                        'http://127.0.0.1:8083/connectors/yeodam-v3-outbox/status',check=False)
        if response.returncode:return None
        data=json.loads(response.stdout)
        return {'connector':data.get('connector',{}).get('state'),
                'tasks':[{'id':x['id'],'state':x['state']}for x in data.get('tasks',[])]}
    initial=state()
    initial_start=initial['State']['StartedAt']
    assert set(initial['NetworkSettings']['Networks'])=={net,c['sourceNetwork']}
    assert reachable('mysql-primary.mysql.svc.cluster.local',3306)
    assert all(reachable('broker'+str(i),9093)for i in range(1,4))
    # Commit the existing fixture's offsets with the unmodified production interval.
    time.sleep(65)
    before=source.request('/connectors/yeodam-v3-outbox/offsets')
    assert before['offsets'] and before['offsets'][0]['offset'].get('file')
    before_records=source.check_wire(source.records(),len(source.expected));assert before_records
    source.trace_write=True
    connected=True
    try:
        docker('network','disconnect',net,worker);connected=False
        started=time.monotonic();result['faultStartedAt']=now()
        assert reachable('mysql-primary.mysql.svc.cluster.local',3306),'Source path accidentally blocked'
        assert all(not reachable('broker'+str(i),9093)for i in range(1,4)),'Kafka fault not effective'
        result['sourceReachableDuringFault']=True;result['allBrokerPathsBlocked']=True
        source.stage_rows(11);expected=len(source.expected)
        observed=source.check_wire(source.records(),len(before_records))
        assert observed and set(observed)==set(before_records),'New rows delivered while Kafka paths blocked'
        result['duringFaultDeliveredNewRecords']=0
        remaining=90-(time.monotonic()-started)
        assert remaining>0,'Fixture setup consumed the fault interval'
        time.sleep(remaining)
        docker('network','connect','--alias','connect-check',net,worker);connected=True
        released=time.monotonic();result['faultReleasedAt']=now();result['observedFaultSeconds']=released-started
        assert reachable('mysql-primary.mysql.svc.cluster.local',3306)
        found=None;observations=[]
        # Local emulation/Java consumer startup is included; this is NOT the production60s goal.
        deadline=released+180
        while time.monotonic()<deadline:
            status_at=now();s=status();observations.append({'at':status_at,'state':s})
            records=source.records();found=source.check_wire(records,expected)
            if found:
                result['allDeliveryObservedAt']=now();result['releaseToAllDeliveryObservedSeconds']=time.monotonic()-released
                break
            time.sleep(2)
        assert found,'Actual accepted Outbox records did not recover within the180s fixture bound'
        final=status();assert final and final['connector']=='RUNNING' and final['tasks']==[{'id':0,'state':'RUNNING'}]
        fresh=state();assert fresh['Id']==initial['Id'] and fresh['State']['StartedAt']==initial_start and fresh['RestartCount']==initial['RestartCount'] and not fresh['State']['OOMKilled']
        log=docker('logs','--timestamps','--since',result['faultStartedAt'],'--tail','5000',worker);events=parse_events(log.stdout+log.stderr,result['faultStartedAt'],now())
        assert events['events'] and any(x['task0Assigned'] for x in events['events']),'No post-fault task assignment evidence'
        assert all(x['scheduledDelayMs']==0 for x in events['events']),'Unexpected scheduled assignment delay'
        result.update(deliveryRecovered=True,initialExpected=len(before_records),afterExpected=expected,
                      recoveredNewIdentities=expected-len(before_records),identityMismatches=0,
                      rawDuplicateDeliveries=sum(v-1 for v in found.values()),
                      traceHeadersPreserved=True,containerRestartDelta=0,sourceOffsetsBefore=before,
                      connectorTaskFinal=final,statusObservations=observations,assignmentEvidence=events)
        print('PASS: active Connector recovered after90s Kafka-only loss; pending Outbox7 delivered, Trace headers retained, observed assignment delay0 and container restart0',flush=True)
    finally:
        if not connected:docker('network','connect','--alias','connect-check',net,worker,check=False)
        persist()
