"""Candidate provenance checks. Registry reads only; no Git write, AWS or deployment."""
import base64
import json
import re
import subprocess
from v3_delivery_contract import COMPONENTS
from v3_release_bundle import prepared, require, parse_json
CLOUD='100-hours-a-week/KTB4-2nd-Cloud'
PATH='deploy/v3/releases/candidate.json'


def file_at(api,ref):
 obj=api(f'repos/{CLOUD}/contents/{PATH}?ref={ref}')
 return parse_json(base64.b64decode(obj['content']))


def verify_components(data,api):
 prepared(data)
 for name,c in data['components'].items():
  repo,image,workflow=COMPONENTS[name];publisher=CLOUD if name=='websocket'else repo
  run=api(f"repos/{publisher}/actions/runs/{c['buildRunId']}")
  require(run.get('id')==c['buildRunId']and run.get('head_sha')==c['buildSourceSha']and run.get('head_branch')=='main'
          and run.get('path')=='.github/workflows/'+workflow and run.get('event')in ['push','workflow_dispatch']
          and run.get('status')=='completed'and run.get('conclusion')=='success'
          and (run.get('repository')or{}).get('full_name')==publisher
          and (run.get('head_repository')or{}).get('full_name')==publisher,'Successful main publisher evidence mismatch: '+name)
  jobs=api(f"repos/{publisher}/actions/runs/{c['buildRunId']}/jobs?filter=latest&per_page=100")
  require(jobs.get('total_count',len(jobs['jobs']))<=100,'Publisher job inventory must be complete')
  marker='Record immutable deployment input' if name=='websocket' else 'Notify Cloud V2 release manifest'
  published=[s for j in jobs['jobs']for s in j.get('steps',[])if s.get('name')==marker]
  require(len(published)==1 and published[0].get('conclusion')=='success','Image publication must actually run, not be skipped: '+name)
  relation=api(f"repos/{repo}/compare/{c['sourceSha']}...main")['status']
  require(relation in ['ahead','identical'],'App source must belong to main history')
  if name=='websocket':
   obj=api(f"repos/{CLOUD}/contents/deploy/v2-production/websocket/source.json?ref={c['buildSourceSha']}")
   reviewed=json.loads(base64.b64decode(obj['content']))
   require(reviewed['sourceRepository']==repo and reviewed['sourceSha']==c['sourceSha']and reviewed['image']=='ghcr.io/100-hours-a-week/'+image+':sha-'+c['sourceSha'],'WebSocket source differs from publisher contract')
  tag='ghcr.io/100-hours-a-week/'+image+':sha-'+c['sourceSha']
  actual=subprocess.check_output(['docker','buildx','imagetools','inspect',tag,'--format','{{.Manifest.Digest}}'],text=True,stderr=subprocess.PIPE).strip()
  require(c['image'].endswith('@'+actual)and re.fullmatch('sha256:[a-f0-9]{64}',actual),'Published SHA tag and immutable digest differ: '+name)


def verify_merged(pr,api):
 require(re.fullmatch('[1-9][0-9]*',str(pr)),'Merged V3 candidate PR required')
 p=api(f'repos/{CLOUD}/pulls/{pr}')
 require(p.get('merged')and p['base']['ref']=='main'and p['base']['repo']['full_name']==CLOUD
         and(p['head'].get('repo')or{}).get('full_name')==CLOUD,'Only merged same-repository candidate PR')
 files=api(f'repos/{CLOUD}/pulls/{pr}/files?per_page=100')
 require(p.get('changed_files')==1 and len(files)==1 and files[0]['filename']==PATH and files[0]['status']=='modified','Candidate PR must modify only the V3 candidate')
 merge=p['merge_commit_sha'];head=p['head']['sha'];require(re.fullmatch('[a-f0-9]{40}',merge),'Exact merge commit')
 data=file_at(api,merge);require(data==file_at(api,head)==file_at(api,'main'),'Candidate changed or superseded')
 runs=api(f'repos/{CLOUD}/actions/workflows/cloud-pr-gate.yml/runs?head_sha={head}&event=pull_request&per_page=100')['workflow_runs']
 runs=[r for r in runs if r.get('head_sha')==head and r.get('head_branch')==p['head']['ref']and r.get('event')=='pull_request'
       and r.get('path')=='.github/workflows/cloud-pr-gate.yml'and(r.get('head_repository')or{}).get('full_name')==CLOUD]
 require(bool(runs),'Matching Cloud PR Gate required');run=max(runs,key=lambda r:r['id'])
 require(run['status']=='completed'and run['conclusion']=='success','Latest Cloud PR Gate must succeed')
 jobs=api(f"repos/{CLOUD}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100")['jobs']
 for name in ['cloud-ci-gate','V3 candidate evidence / V3 candidate evidence']:
  selected=[j for j in jobs if j['name']==name];require(len(selected)==1 and selected[0]['conclusion']=='success','Required successful job: '+name)
 verify_components(data,api)
 return {'candidateVerified':True,'status':'verified','pr':int(pr),'cloudCommit':merge,'ciRunId':run['id'],'candidate':data}
