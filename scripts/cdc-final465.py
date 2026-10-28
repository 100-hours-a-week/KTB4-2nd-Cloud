#!/usr/bin/env python3
"""Final 10/30 RPS runs: exact successful463 bundle and common50 baseline."""
import argparse
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import uuid

ROOT=Path(__file__).resolve().parents[1]
DOCS=ROOT.parent/'KTB4-2nd-Cloud/.local-docs'
BASE=DOCS/'evidence/465-final-rps'
PARENT=DOCS/'evidence/463-cdc-poll/integration'
OLD=DOCS/'evidence/455-index-comparison'
BASELINE='51474607af2aae8212b32bf4093fcde164dfcbda8f01345e88e8eac595559cb3'
OLD30='ab1a0935f6335f0385f5df2cfb2eb4c75c9fce4f05a742d9e81bed3bfe2cfae6'
PROFILES={'rate10':{'rate':10,'preAllocatedVUs':64,'maxVUs':1200,'authActors':40,'poolCount':60,'index':0},
          'rate30':{'rate':30,'preAllocatedVUs':900,'maxVUs':3600,'authActors':120,'poolCount':140,'index':1}}
CASE=None


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def verify_files(root,manifest):
    for name,entry in manifest['files'].items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=entry['renderedSha256']:
            raise RuntimeError('Frozen artifact differs: '+name)


def validate_plan(plan,case,fixed,require_restore=False):
    if case not in PROFILES:raise ValueError('Only final10/final30 are authorized')
    profile=PROFILES[case]
    if any(plan.get(k)!=v for k,v in fixed.items()):raise ValueError('Fixed463 behavior/budgets changed')
    if len(plan.get('rates',[]))!=1:raise ValueError('One immutable Run per case')
    rate=plan['rates'][0]
    if any(rate.get(k)!=profile[k] for k in ('rate','preAllocatedVUs','maxVUs')):raise ValueError('Rate-specific scenario allocation differs')
    if plan.get('authActors')!=profile['authActors'] or plan.get('poolCount')!=profile['poolCount']:
        raise ValueError('Authentication profile differs from unchanged scenario')
    if require_restore:
        b=plan.get('commonBaseline') or {}
        if b.get('sha256')!=BASELINE or b.get('trips')!=2048038 or b.get('journal')!=246512 or b.get('all33TablesAndAutoIncrementEqual') is not True:
            raise ValueError('Restore the same50 baseline, never a rate-specific old backup')


def render_assess(text):
    # All rates now deliberately use the successful50 data; retain every other check.
    old=repr(BASELINE)+" if r['rate']==50 else "+repr(OLD30)
    if text.count(old)!=1:raise ValueError('Parent baseline assertion differs')
    return text.replace(old,repr(BASELINE),1)


def initialize():
    if BASE.exists():raise RuntimeError('Keep existing final-rps evidence')
    parent_manifest=json.loads((PARENT/'bundle-manifest.json').read_text())
    verify_files(PARENT/'runtime/pinned',parent_manifest)
    if json.loads((PARENT/'index50/acceptance.json').read_text()).get('passed') is not True:
        raise RuntimeError('Parent50 must have actual full acceptance')
    scenario=(PARENT/'runtime/pinned/tests/load/scenarios/migration-b-integration.js').read_text()
    if "const vus = ({ 10: 64, 30: 900, 50: 1000 })[rate];" not in scenario or "const actors = ({ 10: 40, 30: 120, 50: 200 })[rate];" not in scenario:
        raise RuntimeError('Actual inherited VU/actor map differs')
    BASE.mkdir();shutil.copytree(PARENT/'runtime',BASE/'runtime');shutil.copytree(PARENT/'helpers',BASE/'helpers')
    manifest=copy.deepcopy(parent_manifest);manifest['issue']=465
    manifest['parentManifestSha256']=hashlib.sha256((PARENT/'bundle-manifest.json').read_bytes()).hexdigest()
    manifest['workloadCodeChanges']='none; rate selected by existing CLI/JS scenario'
    (BASE/'bundle-manifest.json').write_text(json.dumps(manifest,indent=2))
    shutil.copyfile(PARENT/'helpers-manifest.json',BASE/'helpers-manifest.json')
    changed={};(BASE/'tools').mkdir()
    for name in ['report.py','analysis.py','query-deltas.py','assess.py','prepare.py','case-result.py','close.py','failure-auth.py','preserve-failure.py']:
        original=(PARENT/'tools'/name).read_text();text=original
        text=text.replace('evidence/463-cdc-poll/integration','evidence/465-final-rps')
        text=text.replace('KTB4-2nd-Cloud-463','KTB4-2nd-Cloud-465').replace('scripts/cdc-poll463.py','scripts/cdc-final465.py')
        text=text.replace('a.load()','a.load(case)').replace('adapter.load()','adapter.load(sys.argv[1])')
        if name in ('prepare.py','close.py','failure-auth.py','preserve-failure.py'):
            text='import sys\ncase=sys.argv[1]\n'+text
        if name=='prepare.py':
            needle='_,adapter,m,_=a.load(case);'
            if text.count(needle)!=1:raise ValueError('Parent preparation selector differs')
            text=text.replace(needle,needle+'a.previous_restored(case);',1)
        if name=='preserve-failure.py':
            text=text.replace("run='87645d31-f2cd-49d3-8dc2-b41f968515a3'","run=json.loads((a.BASE/case/'plan.json').read_text())['rates'][0]['runId']")
            text=text.replace("a.BASE/'index50/", "a.BASE/case/'")
        if name=='close.py':text=text.replace("(a.BASE/'runtime-final-", "(directory/'runtime-final-")
        if name=='case-result.py':text=text.replace("receipt={'issue':463,","receipt={'issue':465,")
        if name=='assess.py':text=render_assess(text)
        compile(text,name,'exec');(BASE/'tools'/name).write_text(text)
        changed[name]={'before':hashlib.sha256(original.encode()).hexdigest(),'after':hashlib.sha256(text.encode()).hexdigest()}
    (BASE/'tools-manifest.json').write_text(json.dumps(changed,indent=2))
    parent_plan=json.loads((PARENT/'index50/plan.json').read_text());result=[]
    for case,profile in PROFILES.items():
        plan=copy.deepcopy(parent_plan)
        plan.update(issue=465,case=case,comparisonBaselineRun=parent_plan['rates'][0]['runId'],commonBaseline=None,pairedRestoreAttempt=None,
                    authActors=profile['authActors'],poolCount=profile['poolCount'],comparisonScope='Same463 code/data/resources; unchanged rate-specific scenario allocation')
        plan['rates']=[{k:profile[k] for k in ('rate','preAllocatedVUs','maxVUs')}|{'runId':str(uuid.uuid4())}]
        p=BASE/case;p.mkdir();(p/'plan.json').write_text(json.dumps(plan,indent=2));result.append({'case':case,**plan['rates'][0],'poolCount':profile['poolCount']})
    print(json.dumps({'runs':result,'runtimeChanged':False}))


def load(case):
    global CASE
    if case not in PROFILES:raise ValueError('Explicit case required')
    CASE=case
    manifest=json.loads((BASE/'bundle-manifest.json').read_text());verify_files(BASE/'runtime/pinned',manifest)
    helper=json.loads((BASE/'helpers-manifest.json').read_text())
    for name,expected in helper['sha256'].items():
        if hashlib.sha256((BASE/'helpers/scripts'/name).read_bytes()).hexdigest()!=expected:raise RuntimeError('Pinned helper differs')
    a=module('cutover465',BASE/'helpers/scripts/run-migration-cutover458.py');a.BASE=BASE;a.ROOT=BASE/'helpers';a.CASE=case
    plan=json.loads((BASE/case/'plan.json').read_text());validate_plan(plan,case,a.FIXED)
    return a,*a.load()


def previous_restored(case):
    if case=='rate10':return
    path=BASE/'rate10';plan=json.loads((path/'plan.json').read_text());state=json.loads((path/(plan['rates'][0]['runId']+'.state.json')).read_text())
    if state.get('phase')!='completed':raise RuntimeError('Do not overlap Runs')
    flags=json.loads((path/'runtime-final-source.json').read_text())[-1]
    worker=json.loads((path/'runtime-final-kafka.json').read_text())[-1]
    if flags['sourceFlags']!=['0','0'] or flags['targetFlags']!=['1','1'] or worker.get('dedicatedWorkerStopped') is not True:
        raise RuntimeError('Previous Run not restored; strategy success is not required, restoration is')


def deploy(case):
    previous_restored(case);_,adapter,m,success=load(case)
    m.deploy();m.raw('kafka',m.ssm.deploy('scripts/migration-cutover458.py','/run/yeodam-b391/migration-cutover458.py'))
    for host in ('source','target'):m.raw(host,m.ssm.deploy('scripts/b-integration376-reset.py','/run/yeodam-b391/b-integration376-reset.py'))
    text=success.render_bulk((adapter.BUNDLE/'scripts/b-integration376-bulk.py').read_text())
    m.raw('target',["printf '%s' '"+base64.b64encode(text.encode()).decode()+"' | base64 -d > /run/yeodam-b391/b-integration376-bulk.py"])
    m.raw('source',m.ssm.deploy('scripts/b-integration393-resources.py','/run/yeodam-b391/b-integration393-resources.py'))


def execute(case):
    previous_restored(case);a,adapter,m,_=load(case);directory=BASE/case;plan=json.loads((directory/'plan.json').read_text())
    validate_plan(plan,case,a.FIXED,True);run=plan['rates'][0]['runId'];receipt=json.loads((directory/'ready.json').read_text())
    if (directory/(run+'.state.json')).exists():raise RuntimeError('Never repeat an attempted Run')
    keys=['verified','targetBackupReady','privatePoolTransferApproved','all33TablesAndAutoIncrementEqual','indexApplied']
    if receipt.get('runId')!=run or receipt.get('hosts')!=adapter.HOSTS or not all(receipt.get(k) is True for k in keys):
        raise RuntimeError('Exact Run preparation and private transfer approval required')
    if m.ssm.aws('sts','get-caller-identity')['Account']!='483175530259':raise RuntimeError('Wrong account')
    m.execute(plan['rates'][0],PROFILES[case]['index'])


def summarize():
    rows=[]
    for case,path in [('rate10',BASE/'rate10'),('rate30',BASE/'rate30'),('rate50',PARENT/'index50')]:
        plan=json.loads((path/'plan.json').read_text());report=json.loads((path/'report.json').read_text())
        analysis=json.loads((path/'analysis.json').read_text());acceptance=json.loads((path/'acceptance.json').read_text())
        expected=50 if case=='rate50' else PROFILES[case]['rate']
        if report['rate']!=expected or report['runId']!=plan['rates'][0]['runId']:
            raise RuntimeError('Report does not belong to this rate/Run')
        baseline=plan['commonBaseline']
        if baseline['sha256']!=BASELINE or baseline['trips']!=2048038 or baseline['journal']!=246512:
            raise RuntimeError('Do not mix different starting data in the final matrix')
        if report['phase']!='completed':raise RuntimeError('No final result for an unfinished Run')
        rows.append({'rate':report['rate'],'runId':report['runId'],'passed':acceptance['passed'],
                     'input':report['started'],'statuses':report['firstStatuses'],'committed':report['targetRows'],
                     'pending':analysis['requestOffsets']['pending'],'dropped':report['dropped'],
                     'firstResponseP95Ms':report['firstResponseMs'].get('p95'),
                     'commandToCommitP95Ms':report['completions'].get('commandCreatedToCommitObservedMs',{}).get('p95'),
                     'activationSeconds':report['cutoverFromPlannedSeconds'],
                     'beforeFenceCpuMean':analysis['resources']['beforeFence']['cpuMean'],
                     'refresh':report['refreshes'],'csrf':report['csrf'],'redisKeys':report['redisKeys'],
                     'redisEqual':report['redisEqual'],'v1E2E':report['v1E2E'],'v2E2E':report['v2E2E']})
    result={'commonBaselineSha256':BASELINE,'initialTrips':2048038,'initialJournal':246512,
            'workloadCodeChangesFrom463':False,'uiFinalDisplayMeasured':False,'rows':rows,
            'limitations':['One Run per rate, not a repeated statistical capacity benchmark.',
                           'Authentication actor loops are response-dependent; total calls can differ.',
                           'Backend command-to-commit timestamps do not measure final browser display.']}
    (BASE/'final-matrix.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['init','deploy','execute','summarize']);p.add_argument('--case',choices=PROFILES);args=p.parse_args()
    if args.action=='init':initialize();return
    if args.action=='summarize':summarize();return
    if not args.case:raise ValueError('Explicit rate case required')
    (deploy if args.action=='deploy' else execute)(args.case)


if __name__=='__main__':main()
