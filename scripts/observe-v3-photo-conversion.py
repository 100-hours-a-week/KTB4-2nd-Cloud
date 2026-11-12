#!/usr/bin/env python3
"""Read converter memory and aggregate backlog only; never restart, retry or clear work."""
import argparse
import json
from pathlib import Path
from v3_cdc_fault import Kube, DB_SHELL, private_write, require, now

SQL = '''SET SESSION MAX_EXECUTION_TIME=3000;
START TRANSACTION READ ONLY;
SELECT JSON_OBJECT('lane',lane,'status',status,'tasks',COUNT(*),'photos',SUM(remaining_photos),
 'bytes',SUM(remaining_bytes),'oldestAgeSec',GREATEST(0,TIMESTAMPDIFF(SECOND,MIN(created_at),CURRENT_TIMESTAMP)))
FROM yeodam.attachment_conversion_tasks WHERE status IN ('WAITING','RUNNING') GROUP BY lane,status;
COMMIT;
'''
MEMORY = 'for f in memory.current memory.max memory.peak memory.events; do printf "%s=" "$f"; cat "/sys/fs/cgroup/$f"; done'


def aggregate(rows):
    totals = {lane: {'tasks': 0, 'photos': 0, 'bytes': 0, 'oldestAgeSec': 0} for lane in ['LIGHT', 'HEIC']}
    seen = set()
    require(isinstance(rows, list) and len(rows) <= 4, 'Unexpected aggregate result size')
    for row in rows:
        require(set(row) == {'lane','status','tasks','photos','bytes','oldestAgeSec'}, 'Only aggregate fields allowed')
        key = (row['lane'], row['status'])
        require(key[0] in totals and key[1] in ['WAITING','RUNNING'] and key not in seen, 'Invalid or duplicate aggregate group')
        seen.add(key)
        for field in totals[key[0]]:
            require(type(row[field]) is int and row[field] >= 0, 'Nonnegative integer aggregates required')
            if field == 'oldestAgeSec':
                totals[key[0]][field] = max(totals[key[0]][field], row[field])
            else:
                totals[key[0]][field] += row[field]
    return totals


def collect(kube):
    cluster_uid = kube.identity()
    service = kube.get('service', 'mysql', 'mysql-primary')
    require(service['spec']['selector'].get('statefulset.kubernetes.io/pod-name') == 'mysql-0', 'Review changed Primary mapping')
    deployment = kube.get('deployment', 'photo', 'photo-converter')
    pods = kube.get('pods', 'photo', selector=','.join(k+'='+v for k,v in deployment['spec']['selector']['matchLabels'].items()))['items']
    pods = [p for p in pods if not p['metadata'].get('deletionTimestamp')]
    require(len(pods) == 1, 'One converter Pod required; do not observe during replacement')
    pod = pods[0]
    raw = kube.call(['-n','mysql','exec','-i','mysql-0','-c','mysql','--','bash','-c',DB_SHELL], source=SQL)
    backlog = aggregate([json.loads(line) for line in raw.splitlines() if line])
    memory = kube.call(['-n','photo','exec',pod['metadata']['name'],'-c','backend','--','sh','-c',MEMORY])
    return {'capturedAt':now(),'clusterUid':cluster_uid,'pod':pod['metadata']['name'],'podUid':pod['metadata']['uid'],
            'resources':deployment['spec']['template']['spec']['containers'][0]['resources'],
            'containers':[{'restarts':s['restartCount'],'lastTermination':{k:s.get('lastState',{}).get('terminated',{}).get(k) for k in ['reason','exitCode','finishedAt']}} for s in pod['status'].get('containerStatuses',[])],
            'memory':memory,'backlog':backlog,
            'note':'Backlog only; not proof of admission reopening, Kafka drain or business completion'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubeconfig', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists() and not args.output.is_symlink(), 'Use a new private evidence path')
    require(args.output.parent.is_dir() and args.output.parent.stat().st_mode & 0o077 == 0, 'Existing private directory mode700 required')
    result = collect(Kube(args.kubeconfig))
    private_write(args.output, result)
    print(json.dumps({'backlog':result['backlog'],'containers':result['containers'],'readOnly':True}))


if __name__ == '__main__':
    main()
