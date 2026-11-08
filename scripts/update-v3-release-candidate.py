#!/usr/bin/env python3
"""Turn existing successful image notifications into a candidate PR, never a deployment."""
from copy import deepcopy
import importlib.util,json,os,re,subprocess,tempfile,time
from pathlib import Path
import v3_release_bundle as bundle
from v3_release_candidate import PATH,CLOUD,verify_components
from prepare_v3_candidate_helpers import local_charts
ROOT=Path(__file__).resolve().parents[1];BRANCH='release/v3-candidate'


def command(*args,check=True):return subprocess.run(args,cwd=ROOT,text=True,capture_output=True,check=check)
def git(*args,check=True):return command('git',*args,check=check)
def api(path):return json.loads(command('gh','api',path).stdout)


def wait_for_publication(repo,run_id):
 # The notification is sent inside the image job, just before that job completes.
 for attempt in range(25):
  run=api(f'repos/{repo}/actions/runs/{run_id}')
  if run.get('status')=='completed':
   bundle.require(run.get('conclusion')=='success','Notified publisher did not complete successfully');return
  bundle.require(attempt<24,'Publisher still running; retry the candidate workflow after completion')
  time.sleep(5)


def incoming(event,event_name):
 if event_name=='repository_dispatch':
  payload=event.get('client_payload',{})
  bundle.require(set(payload)=={'component','source_repository','source_sha','image','digest','build_run_id'},'Exact image event fields')
  name=payload['component'];bundle.require(name in ['frontend','backend','ai-app'],'Known app publisher only')
  bundle.require(re.fullmatch('[1-9][0-9]*',str(payload['build_run_id'])),'Exact publisher Run')
  component={'sourceRepository':payload['source_repository'],'sourceSha':payload['source_sha'],'image':payload['image'].split(':sha-',1)[0]+'@'+payload['digest'],'buildRunId':int(payload['build_run_id']),'buildSourceSha':payload['source_sha']}
  repo,image,_=bundle.COMPONENTS[name]
  bundle.require(component['sourceRepository']==repo,'Exact publisher repository required')
  bundle.require(re.fullmatch('[a-f0-9]{40}',component['sourceSha'])and re.fullmatch('sha256:[a-f0-9]{64}',payload['digest']),'Exact source and Digest required')
  bundle.require(payload['image']=='ghcr.io/100-hours-a-week/'+image+':sha-'+component['sourceSha'],'SHA tag must match notification source')
  wait_for_publication(repo,component['buildRunId'])
  return name,component
 bundle.require(event_name=='workflow_run','Only image event or WebSocket publisher completion')
 run=event['workflow_run'];bundle.require(type(run.get('id'))is int,'Publisher Run ID')
 actual=api(f"repos/{CLOUD}/actions/runs/{run['id']}")
 bundle.require(actual.get('path')=='.github/workflows/production-websocket-image.yml'and actual.get('head_branch')=='main'and actual.get('status')=='completed'and actual.get('conclusion')=='success','Successful main WebSocket publisher only')
 import base64
 obj=api(f"repos/{CLOUD}/contents/deploy/v2-production/websocket/source.json?ref={actual['head_sha']}")
 source=json.loads(base64.b64decode(obj['content']));tag=source['image']
 bundle.require(re.fullmatch('ghcr.io/100-hours-a-week/yeodam-websocket:sha-[a-f0-9]{40}',tag),'Exact reviewed WebSocket tag')
 digest=command('docker','buildx','imagetools','inspect',tag,'--format','{{.Manifest.Digest}}').stdout.strip()
 return 'websocket',{'sourceRepository':source['sourceRepository'],'sourceSha':source['sourceSha'],'image':tag.split(':sha-',1)[0]+'@'+digest,'buildRunId':actual['id'],'buildSourceSha':actual['head_sha']}


def update(data,name,component):
 proposed=deepcopy(data);proposed['components'][name]=component;bundle.prepared(proposed)
 old=data['components'][name]
 if old['sourceSha']==component['sourceSha']and component['buildRunId']<=old['buildRunId']:return data,False
 if old['sourceSha']!=component['sourceSha']:
  relation=api(f"repos/{component['sourceRepository']}/compare/{old['sourceSha']}...{component['sourceSha']}")['status']
  if relation=='behind':return data,False
  bundle.require(relation=='ahead','Diverged image source requires review')
 new=deepcopy(data);new['components'][name]=component;new=bundle.normalize(new)
 verify_components(new,api)
 return new,new!=data


def ensure_pr():
 existing=json.loads(command('gh','pr','list','--repo',CLOUD,'--base','main','--head',BRANCH,'--state','open','--json','url').stdout)
 if existing:return existing[0]['url']
 body='''## What is this PR? 🔍
- 기능 : V3 전체 앱 이미지와 실행 설정 후보 갱신

## Changes 📝
- 성공한 main 발행 이미지와 Digest 반영
- 공통 BE5종과 AI2종의 이미지 일치 및 기존 설정 유지

## Test CheckList ✅
- [x] 발행 Run과 Registry Digest 확인
- [ ] Cloud PR Gate 확인

## Precaution
병합만으로 배포하지 않음. 별도 운영 승인 후 반영.

## ✔️ Please check if the PR fulfills these requirements
- [x] main 대상
- [x] hotfix 해당 없음
- [x] yarn lint: N/A
'''
 with tempfile.NamedTemporaryFile(mode='w',suffix='.md')as f:
  f.write(body);f.flush();r=command('gh','pr','create','--repo',CLOUD,'--base','main','--head',BRANCH,'--title','[chore] V3 운영 배포 후보 갱신','--body-file',f.name)
 return r.stdout.strip()


def main():
 data=bundle.parse_json((ROOT/PATH).read_text())
 if data.get('state')=='unprepared':print(json.dumps({'status':'bootstrap-required','deployed':False}));return
 event=json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text());name,component=incoming(event,os.environ['GITHUB_EVENT_NAME'])
 bundle.require(not git('status','--porcelain').stdout.strip(),'Clean automation checkout required')
 git('config','user.name','github-actions[bot]');git('config','user.email','41898282+github-actions[bot]@users.noreply.github.com')
 for attempt in range(3):
  git('fetch','origin','main');remote=git('ls-remote','--exit-code','--heads','origin',BRANCH,check=False)
  if remote.returncode==0:
   git('fetch','origin',BRANCH);git('checkout','-B',BRANCH,'origin/'+BRANCH)
   bundle.require(all(p==PATH for p in git('diff','--name-only','origin/main...HEAD').stdout.splitlines()),'Candidate branch contains code changes')
   git('merge','--no-edit','origin/main')
  else:
   bundle.require(remote.returncode==2,'Cannot query candidate ref');git('checkout','-B',BRANCH,'origin/main')
  data=bundle.parse_json((ROOT/PATH).read_text());new,changed=update(data,name,component)
  if changed:
   bundle.validate(bundle.build(new,local_charts(),{'candidatePr':0,'cloudCommit':'0'*40,'cloudGateRunId':0}))
   (ROOT/PATH).write_text(json.dumps(new,indent=2)+'\n');git('add','--',PATH);git('commit','-m','chore: V3 '+name+' 배포 후보 갱신')
  if not git('diff','--name-only','origin/main','HEAD').stdout.strip():print(json.dumps({'status':'unchanged','deployed':False}));return
  if git('push','origin',BRANCH,check=False).returncode==0:
   print(json.dumps({'status':'candidate','pr':ensure_pr(),'deployed':False}));return
 raise ValueError('Candidate ref changed concurrently; no force push; redeliver')


if __name__=='__main__':main()
