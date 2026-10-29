#!/usr/bin/env python3
"""Poll-only overlay on the exact #458 bundle after a successful frozen replay."""
import argparse
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
BASE=DOCS/'evidence/463-cdc-poll/integration'
PARENT=DOCS/'evidence/458-sink-shutdown'
OLD=DOCS/'evidence/455-index-comparison'
CASE='index50'
HELPER_PIN='03c93f7'
HELPERS=('run-migration-cutover458.py','run-migration-index455.py','run-b408-success-version.py',
         'b-integration408-snapshot.py')


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def render_trip_poll(text):
    before="'batch.size':'1','consumer.override.max.poll.records':'1'"
    if text.count(before)!=1:raise ValueError('Frozen travel Sink template differs')
    result=text.replace(before,"'batch.size':'1','consumer.override.max.poll.records':'50'",1)
    compile(result,'b-integration376-kafka.py','exec')
    return result


def replay_gate(directory):
    state=json.loads((directory/'state.json').read_text());analysis=json.loads((directory/'analysis.json').read_text())
    contract=json.loads((directory/'contract.json').read_text())
    baseline=DOCS/'evidence/461-sink-diagnostics/3cde9d90-9b89-49fc-9fac-a6c9e2deeff0/analysis.json'
    old=json.loads(baseline.read_text())
    if contract['tripPoll']!=50 or state.get('phase')!='completed' or not state.get('originalTargetRestored'):
        raise RuntimeError('Complete and restore Poll50 replay first')
    if (not state.get('allOffsetsAtFrozenEnd') or not analysis.get('dataEqual') or analysis['taskCounterResetObserved'] or
            any(v['counterReset'] for v in analysis['sinkUserStatements'].values())):
        raise RuntimeError('Replay integrity failed; no integration')
    if not (analysis['allCommittedFromRegistrationSeconds']<old['allCommittedFromRegistrationSeconds'] and
            analysis['sinkUserStatements']['statement/sql/select']['calls']<old['sinkUserStatements']['statement/sql/select']['calls']):
        raise RuntimeError('No observed processing and SELECT improvement; retain result, no integration')
    return {'replayRun':state['run'],'baselineRun':old['run'],'replayAnalysisSha256':hashlib.sha256((directory/'analysis.json').read_bytes()).hexdigest()}


def initialize(replay):
    receipt=replay_gate(replay)
    if BASE.exists():raise RuntimeError('Keep the existing integration attempt')
    manifest=json.loads((PARENT/'bundle-manifest.json').read_text())
    for name,entry in manifest['files'].items():
        if hashlib.sha256((PARENT/'runtime/pinned'/name).read_bytes()).hexdigest()!=entry['renderedSha256']:
            raise RuntimeError('Parent bundle changed: '+name)
    BASE.mkdir(parents=True)
    shutil.copytree(PARENT/'runtime/pinned',BASE/'runtime/pinned')
    manifest=copy.deepcopy(manifest);manifest['issue']=463
    manifest['parentManifestSha256']=hashlib.sha256((PARENT/'bundle-manifest.json').read_bytes()).hexdigest()
    name='scripts/b-integration376-kafka.py';p=BASE/'runtime/pinned'/name
    text=p.read_text();changed=render_trip_poll(text);p.write_text(changed)
    manifest['pollOverlay']={'file':name,'before':hashlib.sha256(text.encode()).hexdigest(),'after':hashlib.sha256(changed.encode()).hexdigest(),
                             'onlySetting':'consumer.override.max.poll.records:1->50; travel batch.size remains1'}
    manifest['files'][name]['renderedSha256']=manifest['pollOverlay']['after']
    (BASE/'bundle-manifest.json').write_text(json.dumps(manifest,indent=2))
    helper_manifest={}
    for name in HELPERS:
        content=subprocess.check_output(['git','show',HELPER_PIN+':scripts/'+name],cwd=ROOT)
        p=BASE/'helpers/scripts'/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(content)
        helper_manifest[name]=hashlib.sha256(content).hexdigest()
    name='tests/load/fixtures/migration-index455-contract.json'
    content=subprocess.check_output(['git','show',HELPER_PIN+':'+name],cwd=ROOT)
    p=BASE/'helpers'/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(content)
    helper_manifest['../'+name]=hashlib.sha256(content).hexdigest()
    (BASE/'helpers-manifest.json').write_text(json.dumps({'commit':HELPER_PIN,'sha256':helper_manifest},indent=2))
    tools={}
    for name in ('report.py','analysis.py','query-deltas.py','assess.py'):
        original=(PARENT/'tools'/name).read_text()
        text=original.replace('evidence/458-sink-shutdown','evidence/463-cdc-poll/integration')
        text=text.replace('/Users/lee-y.ch/Desktop/yeodam/KTB4-2nd-Cloud-458/scripts/run-migration-cutover458.py',str(ROOT/'scripts/cdc-poll463.py'))
        text=text.replace('original,m,_=adapter.load()','_,original,m,_=adapter.load()')
        compile(text,name,'exec');p=BASE/'tools'/name;p.parent.mkdir(exist_ok=True);p.write_text(text)
        tools[name]={'before':hashlib.sha256(original.encode()).hexdigest(),'after':hashlib.sha256(text.encode()).hexdigest()}
    (BASE/'report-tools-manifest.json').write_text(json.dumps(tools,indent=2))
    plan=json.loads((PARENT/'index50/plan.json').read_text())
    plan.update(issue=463,comparisonBaselineRun=plan['rates'][0]['runId'],commonBaseline=None,pairedRestoreAttempt=None,
                comparisonScope='Exact458 flow with travel Poll1->50 only; replay integrity and speed gate passed')
    plan['rates'][0]['runId']=str(uuid.uuid4())
    d=BASE/'index50';d.mkdir();(d/'plan.json').write_text(json.dumps(plan,indent=2))
    (BASE/'replay-gate.json').write_text(json.dumps(receipt,indent=2))
    print(json.dumps({'runId':plan['rates'][0]['runId'],'directory':str(d),'runtimeChanged':False}))


def load():
    manifest=json.loads((BASE/'helpers-manifest.json').read_text())
    for name,expected in manifest['sha256'].items():
        if hashlib.sha256((BASE/'helpers/scripts'/name).read_bytes()).hexdigest()!=expected:raise RuntimeError('Pinned helper changed')
    a=module('cutover463',BASE/'helpers/scripts/run-migration-cutover458.py')
    a.BASE=BASE;a.ROOT=BASE/'helpers'
    return a,*a.load()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['init','deploy','execute']);p.add_argument('--replay',type=Path);args=p.parse_args()
    if args.action=='init':
        if not args.replay:raise ValueError('Exact completed replay evidence required')
        initialize(args.replay);return
    a,adapter,m,success=load()
    # Reuse the reviewed parent driver without modifying another checkout.
    # Its exact-run receipt still requires fresh baseline and private-pool approval.
    import sys
    sys.argv=[sys.argv[0],args.action];a.main()


if __name__=='__main__':main()
