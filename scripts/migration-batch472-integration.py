#!/usr/bin/env python3
"""Conditional900s cutover on successful463 settings, changing batch image only."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import time

ROOT=Path(__file__).resolve().parents[1]
DOCS=ROOT.parent/'KTB4-2nd-Cloud/.local-docs'
ISSUE=DOCS/'evidence/472-batch-commit'
BASE=ISSUE/'integration'
PARENT=DOCS/'evidence/463-cdc-poll/integration'
OLD=DOCS/'evidence/455-index-comparison'
CASE='index50'

def module(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

def prerequisite():
 selection=json.loads((ISSUE/'selected-diagnostic.json').read_text()) if (ISSUE/'selected-diagnostic.json').exists() else {'directory':'diagnostic'}
 if selection['directory'] not in ('diagnostic','diagnostic-retry1'):raise RuntimeError('Unknown diagnostic directory')
 d=ISSUE/selection['directory']/'index50';p=json.loads((d/'plan.json').read_text());state=json.loads((d/(p['rates'][0]['runId']+'.state.json')).read_text())
 if state.get('phase')!='completed' or state.get('runtimeRestored') is not True:raise RuntimeError('Diagnosis not preserved/restored')
 result=json.loads((ISSUE/'diagnostic-benefit.json').read_text())
 if result.get('diagnosticRunId')!=p['rates'][0]['runId'] or result.get('eligibleForIntegration') is not True:raise RuntimeError('No evidenced diagnostic benefit')
 return result

def validate_plan(plan,control=None):
 control=control or json.loads((PARENT/CASE/'plan.json').read_text())
 changes={'issue','rates','backendImage','backendCommit','imageWorkflow','commonBaseline','pairedRestoreAttempt',
          'comparisonBaselineRun','comparisonScope','maxTransactionRecords','runtimeImageParent'}
 if set(plan)-changes!=set(control)-changes:raise RuntimeError('Integration plan fields drift')
 for key in set(control)-changes:
  if plan[key]!=control[key]:raise RuntimeError('Frozen successful900s setting differs: '+key)
 expected=copy.deepcopy(control['rates'][0]);expected['runId']=plan['rates'][0]['runId']
 if plan['rates']!=[expected] or expected['runId']==control['rates'][0]['runId']:raise RuntimeError('Run/rate allocation differs')
 if plan['durationSeconds']!=900 or plan['cutoverSeconds']!=360 or plan['maxTransactionRecords']!=10:raise RuntimeError('Input/batch/boundary differs')
 if plan['runtimeImageParent']!=control['backendImage']:raise RuntimeError('Runtime parent differs')
 receipt=json.loads((ISSUE/'image-receipt.json').read_text())
 for key in ['backendCommit','backendImage','imageWorkflow']:
  if plan[key]!=receipt['workflow' if key=='imageWorkflow' else key]:raise RuntimeError('Published artifact differs')

def driver():
 a=module('batch472base',ROOT/'scripts/migration-batch472.py');d=a.driver();d.BASE=BASE;d.validate_plan=validate_plan;return d

def load():return driver().load()

def previous_restored(case):
 if case!=CASE:raise RuntimeError('Only authorized50 integration')
 prerequisite()

def initialize():
 prerequisite()
 if BASE.exists():raise RuntimeError('Keep existing integration attempt')
 plan=json.loads((ISSUE/'planned-integration-plan.json').read_text());validate_plan(plan)
 a=module('init472base',ROOT/'scripts/migration-batch472.py')
 manifest=json.loads((PARENT/'bundle-manifest.json').read_text());a.driver().verify(PARENT/'runtime/pinned',manifest)
 if json.loads((PARENT/CASE/'acceptance.json').read_text()).get('passed') is not True:raise RuntimeError('Successful parent missing')
 BASE.mkdir();shutil.copytree(PARENT/'runtime',BASE/'runtime');shutil.copytree(PARENT/'helpers',BASE/'helpers');shutil.copyfile(PARENT/'helpers-manifest.json',BASE/'helpers-manifest.json')
 name='scripts/b-integration376-app.py';p=BASE/'runtime/pinned'/name;before=p.read_text();old=json.loads((PARENT/CASE/'plan.json').read_text())['backendImage']
 if before.count(old)!=1:raise RuntimeError('Image anchor differs')
 after=before.replace(old,plan['backendImage']);compile(after,name,'exec');p.write_text(after)
 manifest['issue']=472;manifest['files'][name]['renderedSha256']=hashlib.sha256(after.encode()).hexdigest();manifest['batch472Overlay']={'changedRuntimeFiles':[name],'backendCommit':plan['backendCommit'],'image':plan['backendImage'],'observer':'Successful463 full flow; no JFR overlay'}
 (BASE/'bundle-manifest.json').write_text(json.dumps(manifest,indent=2));a.install_candidate_pull(BASE)
 (BASE/CASE).mkdir();(BASE/CASE/'plan.json').write_text(json.dumps(plan,indent=2))
 source=(ISSUE/'diagnostic/tools/prepare.py').read_text()
 if source.count('scripts/migration-batch472.py')!=1:raise RuntimeError('Preparation import anchor differs')
 source=source.replace('scripts/migration-batch472.py','scripts/migration-batch472-integration.py')
 compile(source,'prepare-integration472','exec');(BASE/'tools').mkdir();(BASE/'tools/prepare.py').write_text(source)
 print(json.dumps({'phase':'initialized','runId':plan['rates'][0]['runId'],'runtimeChanged':False}))

def run():
 prerequisite();p=BASE/CASE;plan=json.loads((p/'plan.json').read_text());validate_plan(plan);run=plan['rates'][0]['runId']
 if (p/'execute-invocation.json').exists() or (p/(run+'.state.json')).exists():raise RuntimeError('Never repeat started integration')
 ready=json.loads((p/'ready.json').read_text());scope=json.loads((ISSUE/'scoped-transfer-requests.json').read_text())
 request=next(v for v in scope['requests'] if v['phase']=='integration900')
 if scope.get('approved') is not True or request['runId']!=run or request['sessions']!=220 or request['transfersAllowed']!=1:raise RuntimeError('Exact approved integration pool scope missing')
 if ready.get('runId')!=run or not all(ready.get(k) is True for k in ['verified','targetBackupReady','all33TablesAndAutoIncrementEqual','indexApplied']):raise RuntimeError('Restored clone baseline missing')
 if plan['commonBaseline']['sha256']!='51474607af2aae8212b32bf4093fcde164dfcbda8f01345e88e8eac595559cb3':raise RuntimeError('Wrong initial data')
 _,_,m,_=load()
 if m.ssm.aws('sts','get-caller-identity')['Account']!='483175530259':raise RuntimeError('Wrong account')
 module('check472image',ROOT/'scripts/migration-batch472.py').verify_candidate_cached(BASE)
 ready.update(privatePoolTransferApproved=True,privatePoolTransferApproval={**request,'approved':True,'approvedAt':scope['approvedAt']});(p/'ready.json').write_text(json.dumps(ready,indent=2))
 driver().source_http_ready(m)
 (p/'execute-invocation.json').write_text(json.dumps({'runId':run,'at':time.time(),'durationSeconds':900,'writerSwitch':True}))
 m.execute(plan['rates'][0],2)

def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['init','deploy','run']);a=p.parse_args()
 if a.action=='init':initialize();return
 prerequisite();validate_plan(json.loads((BASE/CASE/'plan.json').read_text()))
 if a.action=='deploy':
  driver().deploy();module('check472image',ROOT/'scripts/migration-batch472.py').verify_candidate_cached(BASE)
 else:run()

if __name__=='__main__':main()
