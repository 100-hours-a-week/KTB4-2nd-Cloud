#!/usr/bin/env python3
"""Compare bounded trip transaction batches against the instrumented467 control."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import uuid

ROOT=Path(__file__).resolve().parents[1]
DOCS=ROOT.parent/'KTB4-2nd-Cloud/.local-docs'
ISSUE=DOCS/'evidence/472-batch-commit'
SELECTION=ISSUE/'selected-diagnostic.json'
attempt=json.loads(SELECTION.read_text())['directory'] if SELECTION.exists() else 'diagnostic'
if attempt not in ('diagnostic','diagnostic-retry1'):raise RuntimeError('Unknown diagnostic selection')
BASE=ISSUE/attempt
CONTROL=DOCS/'evidence/467-java-mysql'
CASE='index50'
OLD=DOCS/'evidence/455-index-comparison'
EXPECTED_BASELINE='51474607af2aae8212b32bf4093fcde164dfcbda8f01345e88e8eac595559cb3'

def module(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

def validate_plan(plan,control=None):
 control=control or json.loads((CONTROL/CASE/'plan.json').read_text())
 changes={'issue','rates','backendImage','backendCommit','imageWorkflow','commonBaseline','pairedRestoreAttempt',
          'comparisonBaselineRun','comparisonScope','observationOnly','maxTransactionRecords','runtimeImageParent'}
 if set(plan)-changes!=set(control)-changes:raise RuntimeError('Plan field set drift')
 for k in set(control)-changes:
  if plan[k]!=control[k]:raise RuntimeError('Frozen control setting differs: '+k)
 expected=copy.deepcopy(control['rates'][0]);expected['runId']=plan['rates'][0]['runId']
 if plan['rates']!=[expected] or expected['runId']==control['rates'][0]['runId']:raise RuntimeError('Rate/VU/control Run drift')
 if plan.get('maxTransactionRecords')!=10:raise RuntimeError('Transaction bound differs')
 if plan.get('runtimeImageParent')!=control['backendImage']:raise RuntimeError('Runtime parent differs')
 if not re.fullmatch('[0-9a-f]{40}',plan['backendCommit']):raise RuntimeError('Pin new BE Commit')
 if not re.fullmatch(r'ghcr.io/100-hours-a-week/yeodam-backend@sha256:[0-9a-f]{64}',plan['backendImage']):raise RuntimeError('Pin new image digest')
 if plan['backendCommit']==control['backendCommit'] or plan['backendImage']==control['backendImage']:raise RuntimeError('Candidate not built')

def driver():
 a=module('diagnostic472base',ROOT/'scripts/diagnose-javamysql467.py')
 a.ROOT=ROOT;a.BASE=BASE;a.validate_plan=validate_plan
 return a

def load():return driver().load()

def previous_restored(case):return driver().previous_restored(case)

def verify_candidate_cached(base=None):
 base=base or BASE;d=driver();d.BASE=base
 _,_,m,_=d.load();plan=json.loads((base/CASE/'plan.json').read_text())
 code='candidate='+repr(plan['backendImage'])+'\nparent='+repr(plan['runtimeImageParent'])+'\n'+'''import json,subprocess
read=lambda image:json.loads(subprocess.check_output(['docker','image','inspect',image],text=True))[0]
new=read(candidate);old=read(parent)
assert candidate in new['RepoDigests'], 'Candidate digest not cached'
assert new['RootFS']['Layers'][:len(old['RootFS']['Layers'])]==old['RootFS']['Layers']
for key in ['Env','User','WorkingDir','Entrypoint','Cmd']:assert new['Config'].get(key)==old['Config'].get(key)
print(json.dumps({'candidateCached':True,'image':candidate,'parentRuntimeLayersAndConfigEqual':True}))
'''
 compile(code,'candidate-image-check','exec')
 values=m.records(m.raw('source',["python3 - <<'IMAGE472'\n"+code+'\nIMAGE472']))
 (base/CASE/'candidate-image-ready.json').write_text(json.dumps(values,indent=2));return values

def candidate_pull_source(text):
 old="    SourceFileLoader('prep444', '/var/lib/yeodam/migration444/prepare.py').load_module().ensure_image()"
 if text.count(old)!=1:raise ValueError('Pinned image helper anchor differs')
 new="    preparation=SourceFileLoader('prep444', '/var/lib/yeodam/migration444/prepare.py').load_module()\n    preparation.IMAGE=IMAGE\n    preparation.ensure_image()"
 return text.replace(old,new,1)

def install_candidate_pull(base=None):
 base=base or BASE
 manifest=json.loads((base/'bundle-manifest.json').read_text());name='scripts/b2-pilot370-host.py'
 if name in manifest['batch472Overlay']['changedRuntimeFiles']:raise RuntimeError('Already applied; keep provenance')
 p=base/'runtime/pinned'/name;before=p.read_text();after=candidate_pull_source(before);compile(after,name,'exec');p.write_text(after)
 manifest['files'][name]['renderedSha256']=hashlib.sha256(after.encode()).hexdigest()
 manifest['batch472Overlay']['changedRuntimeFiles'].append(name)
 manifest['batch472Overlay']['imagePreparationFix']={'before':hashlib.sha256(before.encode()).hexdigest(),'after':hashlib.sha256(after.encode()).hexdigest(),'reason':'Pass candidate digest into existing scoped registry preparation; no workload change'}
 (base/'bundle-manifest.json').write_text(json.dumps(manifest,indent=2))

def observer_ready():
 d=driver();_,_,m,_=d.load();plan=json.loads((BASE/CASE/'plan.json').read_text());run=plan['rates'][0]['runId']
 validate_plan(plan)
 if (BASE/'jfr-probe.json').exists():raise RuntimeError('Keep completed observer receipt')
 old=json.loads((CONTROL/CASE/'plan.json').read_text())['rates'][0]['runId']
 reader=json.loads((CONTROL/'observer-manifest.json').read_text())['readerClassSha256']
 code='''import importlib.util,json,hashlib,subprocess,shutil
from pathlib import Path
s=importlib.util.spec_from_file_location('host467','/run/yeodam-b391/javamysql467-host.py');h=importlib.util.module_from_spec(s);s.loader.exec_module(h)
root=Path('/run/yeodam-javamysql467');run=RUN;old=OLD
reader=root/old/'app/Jfr467Summary.class'
assert hashlib.sha256(reader.read_bytes()).hexdigest()==READER_SHA
profiles={}
for role in ['app','consumer']:
 h.setup_jfr(run,role)
 current=root/run/role/'settings.jfc';previous=root/old/role/'settings.jfc'
 assert current.read_bytes()==previous.read_bytes(), 'Observer profile differs'
 profiles[role]=hashlib.sha256(current.read_bytes()).hexdigest()
target=root/run/'app';shutil.copyfile(reader,target/'Jfr467Summary.class');(target/'Jfr467Summary.class').chmod(0o444)
image=IMAGE
common=['docker','run','--rm','--network','none','--cpus','1','--mount','type=bind,source='+str(target)+',target=/run/jfr467','--entrypoint','java']
p=subprocess.run(common+['--memory','256m',image,'-Xmx32m','-XX:StartFlightRecording=settings=/run/jfr467/settings.jfc,filename=/run/jfr467/probe.jfr,dumponexit=true','-version'],capture_output=True,text=True)
if p.returncode:raise RuntimeError('JFR startup probe failed; no input')
p=subprocess.run(common+['--memory','1024m',image,'-Xmx768m','-cp','/run/jfr467','Jfr467Summary','/run/jfr467/probe.jfr','0','9223372036854775807'],capture_output=True,text=True)
if p.returncode:raise RuntimeError('JFR reader probe failed; no input')
report=json.loads(p.stdout)
if report['deniedEventCounts']:raise RuntimeError('Sensitive JFR event; no input')
print(json.dumps({'runId':run,'exitCode':0,'deniedEvents':report['deniedEventCounts'],'readerClassSha256':READER_SHA,'profilesMatchControl':profiles,'probeBytes':(target/'probe.jfr').stat().st_size}))
'''.replace('READER_SHA',repr(reader)).replace('RUN',repr(run)).replace('OLD',repr(old)).replace('IMAGE',repr(plan['runtimeImageParent']))
 records=m.records(m.raw('source',["python3 - <<'OBSERVER472'\n"+code+'\nOBSERVER472']))
 (BASE/'jfr-probe.json').write_text(json.dumps(records,indent=2));print(json.dumps(records[-1]))

def initialize(commit,image,workflow):
 if BASE.exists():raise RuntimeError('Keep existing472 attempt; no overwrite/replacement')
 control=json.loads((CONTROL/CASE/'plan.json').read_text())
 oldstate=json.loads((CONTROL/CASE/(control['rates'][0]['runId']+'.state.json')).read_text())
 if oldstate.get('phase')!='completed' or oldstate.get('runtimeRestored') is not True:raise RuntimeError('Control not restored')
 manifest=json.loads((CONTROL/'bundle-manifest.json').read_text())
 driver().verify(CONTROL/'runtime/pinned',manifest)
 plan=copy.deepcopy(control)
 plan.update(issue=472,backendCommit=commit,backendImage=image,imageWorkflow=workflow,commonBaseline=None,
             pairedRestoreAttempt=None,comparisonBaselineRun=control['rates'][0]['runId'],
             comparisonScope='467 instrumented180s; bounded trip batch commits only',
             observationOnly='Same JFR/MySQL checkpoints as467; no extra HTTP load',
             maxTransactionRecords=10,runtimeImageParent=control['backendImage'])
 plan['rates'][0]['runId']=str(uuid.uuid4());validate_plan(plan,control)
 BASE.mkdir();shutil.copytree(CONTROL/'runtime',BASE/'runtime');shutil.copytree(CONTROL/'helpers',BASE/'helpers')
 shutil.copyfile(CONTROL/'helpers-manifest.json',BASE/'helpers-manifest.json')
 name='scripts/b-integration376-app.py';p=BASE/'runtime/pinned'/name;text=p.read_text()
 if text.count(control['backendImage'])!=1:raise RuntimeError('Image anchor drift')
 updated=text.replace(control['backendImage'],image);compile(updated,name,'exec');p.write_text(updated)
 manifest['issue']=472;manifest['files'][name]['renderedSha256']=hashlib.sha256(updated.encode()).hexdigest()
 manifest['batch472Overlay']={'changedRuntimeFiles':[name],'backendCommit':commit,'image':image,'runtimeParent':control['backendImage']}
 (BASE/'bundle-manifest.json').write_text(json.dumps(manifest,indent=2))
 d=BASE/CASE;d.mkdir();(d/'plan.json').write_text(json.dumps(plan,indent=2))
 tools=BASE/'tools';tools.mkdir()
 source=(CONTROL/'tools/prepare.py').read_text().replace('KTB4-2nd-Cloud-467','KTB4-2nd-Cloud-472').replace('scripts/diagnose-javamysql467.py','scripts/migration-batch472.py')
 compile(source,'prepare472','exec');(tools/'prepare.py').write_text(source)
 install_candidate_pull()
 SELECTION.write_text(json.dumps({'directory':BASE.name,'runId':plan['rates'][0]['runId']},indent=2))
 print(json.dumps({'runId':plan['rates'][0]['runId'],'inputSeconds':180,'writerSwitch':False,'runtimeChanged':False}))

def main():
 global BASE
 p=argparse.ArgumentParser();p.add_argument('action',choices=['init','deploy','observer-ready','fix-image-pull','run','resume-before-input','close','analyze']);p.add_argument('--backend-commit');p.add_argument('--backend-image');p.add_argument('--workflow',type=int);p.add_argument('--pull',action='store_true');p.add_argument('--attempt',choices=['diagnostic','diagnostic-retry1']);a=p.parse_args()
 if a.attempt:
  if a.action=='init' and a.attempt=='diagnostic-retry1':
   closed=json.loads((ISSUE/'diagnostic/index50/pre-input-failure-closeout.json').read_text())
   if closed.get('actualInputStarted') is not False or closed.get('runtimeRestored') is not True:raise RuntimeError('Original attempt not safely closed')
  BASE=ISSUE/a.attempt
 if a.action=='init':
  if not all((a.backend_commit,a.backend_image,a.workflow)):raise RuntimeError('Published image receipt required')
  initialize(a.backend_commit,a.backend_image,a.workflow);return
 d=driver();validate_plan(json.loads((BASE/CASE/'plan.json').read_text()))
 if a.action=='deploy':d.deploy();verify_candidate_cached()
 elif a.action=='fix-image-pull':install_candidate_pull()
 elif a.action=='observer-ready':observer_ready()
 elif a.action in ('run','resume-before-input'):
  verify_candidate_cached()
  r=module('run472',ROOT/'scripts/javamysql467-run.py');r.load_driver=lambda root:d;r.run(ROOT,resume_after_transfer=a.action=='resume-before-input')
 elif a.action=='close':module('close472',ROOT/'scripts/javamysql467-close.py').main(d,'diagnostics472')
 else:
  r=module('analyze472',ROOT/'scripts/analyze-javamysql467.py');r.BASE=BASE/CASE;r.ARCHIVE_NAMESPACE='diagnostics472';r.main(['--pull'] if a.pull else [])

if __name__=='__main__':main()
