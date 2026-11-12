#!/usr/bin/env python3
"""GET-only installation/activation evidence; never installs, changes replicas or reads Secrets."""
import argparse
import json
from decimal import Decimal
from v3_cdc_fault import Kube, require
from v3_autoscaling import validate_prerequisites

def cpu(value):
    return Decimal(value[:-1])/1000 if value.endswith('m') else Decimal(value)
def memory(value):
    for unit,factor in [('Gi',1024**3),('Mi',1024**2),('Ki',1024)]:
        if value.endswith(unit):return int(Decimal(value[:-len(unit)])*factor)
    return int(value)
def capacity(nodes,pods):
    result=[]
    for node in nodes:
        labels=node['metadata'].get('labels',{})
        if labels.get('yeodam.io/role')!='worker' or labels.get('yeodam.io/stage')!='v3':continue
        if node['metadata'].get('deletionTimestamp') or node['spec'].get('unschedulable') or any(t.get('effect')in ('NoSchedule','NoExecute')and not(t.get('key')=='yeodam.io/role'and t.get('value')=='worker')for t in node['spec'].get('taints',[])) or not any(c['type']=='Ready'and c['status']=='True'for c in node.get('status',{}).get('conditions',[])):continue
        alloc=node['status']['allocatable'];usedcpu=Decimal(0);usedmem=0;count=0
        for p in pods:
            if p['spec'].get('nodeName')!=node['metadata']['name']or p.get('status',{}).get('phase')in ('Succeeded','Failed'):continue
            count+=1
            # Scheduler-effective requests include init container maxima and Pod overhead.
            containers=p['spec'].get('containers',[]);init=p['spec'].get('initContainers',[])
            for resource,parse in [('cpu',cpu),('memory',memory)]:
                ordinary=sum(parse(c.get('resources',{}).get('requests',{}).get(resource,'0'))for c in containers)
                initial=max([parse(c.get('resources',{}).get('requests',{}).get(resource,'0'))for c in init]or[0])
                val=max(ordinary,initial)+parse(p['spec'].get('overhead',{}).get(resource,'0'))
                if resource=='cpu':usedcpu+=val
                else:usedmem+=val
        slots=max(0,min(int(alloc['pods'])-count,int((cpu(alloc['cpu'])-usedcpu)/Decimal('0.025')),(memory(alloc['memory'])-usedmem)//(64*1024**2)))
        result.append({'node':node['metadata']['name'],'uid':node['metadata']['uid'],'managementPodSlots':slots})
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--kubeconfig',required=True);p.add_argument('--after-install',action='store_true');p.add_argument('--converter',action='store_true');a=p.parse_args()
    k=Kube(a.kubeconfig);uid=k.identity()
    nodes=k.get('nodes')['items'];pods=json.loads(k.call(['get','pods','-A','-o','json']))['items']
    rooms=capacity(nodes,pods)
    result={'kubeSystemUid':uid,'cpuWorkerCapacity':rooms,'fourManagementPodsFitWithoutNewNode':sum(n['managementPodSlots']for n in rooms)>=4,'newEc2PermittedByThisInspection':False,'liveChanges':False}
    if a.after_install:
        items=k.get('apiservices')['items']
        for namespace in ['frontend','auth','trip','photo']:
            for kind in ['deployments','horizontalpodautoscalers']:
                items+=k.get(kind,namespace)['items']
        if a.converter:
            items+=k.get('scaledobjects.keda.sh','photo')['items']
            items+=k.get('kafkatopics.kafka.strimzi.io','kafka')['items']
        result['activation']=validate_prerequisites(items,a.converter)
    print(json.dumps(result,indent=2))
if __name__=='__main__':main()
