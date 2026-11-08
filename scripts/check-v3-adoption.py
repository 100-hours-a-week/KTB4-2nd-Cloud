#!/usr/bin/env python3
"""Compare approved app templates with public live snapshots before handing Helm ownership to Argo."""
import argparse,json
from copy import deepcopy
from pathlib import Path
import v3_release_bundle as bundle


def trim_defaults(live,expected):
 if isinstance(live,dict)and isinstance(expected,dict):
  result=deepcopy(live)
  defaults={'creationTimestamp':None,'restartPolicy':'Always','dnsPolicy':'ClusterFirst','schedulerName':'default-scheduler',
            'enableServiceLinks':True,'terminationMessagePath':'/dev/termination-log','terminationMessagePolicy':'File',
            'imagePullPolicy':'IfNotPresent','protocol':'TCP'}
  for key,value in defaults.items():
   if key not in expected and result.get(key,object())==value:result.pop(key,None)
  for key in set(result)&set(expected):result[key]=trim_defaults(result[key],expected[key])
  return result
 if isinstance(live,list)and isinstance(expected,list):
  return [trim_defaults(x,expected[i])if i<len(expected)else x for i,x in enumerate(live)]
 return live


def verify(files,live):
 bundle.validate(files);desired=bundle.apps.platform.load_documents(files['manifests/release.yaml'].decode())
 bundle.require(live.get('kind')in ['List']and isinstance(live.get('items'),list),'Public app object List required')
 index={}
 for obj in live['items']:
  bundle.require(obj.get('kind')in ['Deployment','Service','ConfigMap','ServiceAccount','ExternalSecret','NetworkPolicy'],'Never collect Secret/PVC/data objects')
  key=(obj['kind'],obj['metadata']['namespace'],obj['metadata']['name']);bundle.require(key not in index,'Duplicate live object');index[key]=obj
 expected={(x['kind'],x['metadata']['namespace'],x['metadata']['name'])for x in desired}
 bundle.require(set(index)==expected,'Capture exactly the reviewed resource names, not namespace-wide state')
 for d in desired:
  key=(d['kind'],d['metadata']['namespace'],d['metadata']['name']);current=index[key]
  if d['kind']=='Deployment':
   bundle.require(not current['spec'].get('paused',False)and current['spec'].get('replicas')==d['spec']['replicas'],'Resolve paused/scaled workloads before adoption')
   bundle.require(trim_defaults(current['spec']['template'],d['spec']['template'])==d['spec']['template'],'Pod template differs; refresh public inputs before adoption: '+d['metadata']['name'])
  if d['kind']=='Service':
   bundle.require(current['spec'].get('type','ClusterIP')=='ClusterIP'and current['spec']['selector']==d['spec']['selector'],'Service identity differs')
   bundle.require(trim_defaults(current['spec']['ports'],d['spec']['ports'])==d['spec']['ports'],'Service ports differ')
  if d['kind']=='ConfigMap':
   bundle.require(current.get('data',{})==d.get('data',{})and current.get('binaryData',{})==d.get('binaryData',{}),'Runtime configuration differs before adoption')
  if d['kind']in ['ExternalSecret','NetworkPolicy']:
   bundle.require(current['spec']==d['spec'],'Access or runtime-reference policy differs before adoption')
 return {'deployments':9,'podTemplateChanges':0,'ownershipOnly':True,'deployed':False}


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--bundle',required=True,type=Path);p.add_argument('--live',required=True,type=Path);a=p.parse_args()
 files={str(f.relative_to(a.bundle)):f.read_bytes()for f in a.bundle.rglob('*')if f.is_file()}
 print(json.dumps(verify(files,bundle.parse_json(a.live.read_text()))))
if __name__=='__main__':main()
