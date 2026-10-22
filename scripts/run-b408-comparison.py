#!/usr/bin/env python3
"""Compare only dump pacing, with the complete existing B handoff and audits."""
import argparse
import importlib.util
import json
from pathlib import Path
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=ROOT.parent/'KTB4-2nd-Cloud/.local-docs/evidence/408-snapshot-contention'
IMAGE='ghcr.io/100-hours-a-week/yeodam-backend@sha256:89b24d02225c8fb6143a4ee323e9b3221c18676c0ab14056b38a6edc608ad4e1'


def runner(case):
    spec=importlib.util.spec_from_file_location('runner408',ROOT/'scripts/b-integration376-run.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    m.EVIDENCE=m.ssm.EVIDENCE=EVIDENCE/case
    return m


def initialize():
    EVIDENCE.mkdir(parents=True,exist_ok=True)
    path=EVIDENCE/'contract.json'
    if path.exists():raise RuntimeError('Already initialized; preserve Run IDs')
    contract={'writeRate':30,'readListRate':1,'readDetailRate':1,'duration':900,
              'latestFenceSeconds':600,'cdcBudgetSeconds':45,'activationBudgetSeconds':50,
              'redisAuditSeconds':1010,'e2eSeconds':1020,'e2eRequestsPerVersion':30,
              'sourceConcurrency':4,'targetConcurrency':8,'maxPollRecords':32,
              'backendImage':IMAGE,'limitedRateFraction':0.5,
              'onlyTreatment':'dump stream byte rate; no priority or server setting changes',
              'dataAndCacheNotReset':True}
    for label in ('control','limited'):
        directory=EVIDENCE/label;directory.mkdir()
        case={'rate':30,'runId':str(uuid.uuid4()),'candidate':'finite-watermark',
              'readProbes':True,'connectorIndex':0,'latestFenceSeconds':600}
        if label=='control':case['snapshotBytesPerSecond']=0
        (directory/'plan.json').write_text(json.dumps({'backendImage':IMAGE,'rates':[case]},indent=2))
        contract[label+'RunId']=case['runId']
    path.write_text(json.dumps(contract,indent=2));print(json.dumps(contract))


def configure_limited():
    plan=json.loads((EVIDENCE/'limited/plan.json').read_text())
    if 'snapshotBytesPerSecond' in plan['rates'][0]:raise RuntimeError('Limited rate already fixed')
    control=json.loads((EVIDENCE/'control/plan.json').read_text())['rates'][0]
    state=json.loads((EVIDENCE/'control'/(control['runId']+'.state.json')).read_text())
    if state['phase']!='completed':raise RuntimeError('Control not finalized')
    stats=json.loads((EVIDENCE/'control/snapshot-stream.json').read_text())
    if stats['configuredBytesPerSecond']!=0 or stats['streamSeconds']<=0:raise RuntimeError('Invalid control measurement')
    rate=int(stats['uncompressedBytes']/stats['streamSeconds']*0.5)
    if rate<1:raise RuntimeError('Invalid fixed rate')
    plan['rates'][0]['snapshotBytesPerSecond']=rate
    plan['rateSelectedBeforeInput']={'at':time.time(),'controlBytes':stats['uncompressedBytes'],
        'controlStreamSeconds':stats['streamSeconds'],'fraction':0.5}
    (EVIDENCE/'limited/plan.json').write_text(json.dumps(plan,indent=2));print(json.dumps({'fixedBytesPerSecond':rate}))


def ensure_e2e_prepared(label):
    verdict=json.loads((EVIDENCE/label/'e2e-preflight.json').read_text())
    if not verdict.get('healthy') or not verdict.get('registryPullAuthorized'):
        raise RuntimeError('V2 E2E preparation failed; no measured input')
    if time.time()-verdict['finishedAt']>1800:raise RuntimeError('E2E preflight expired; no input')


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('init','configure-limited','deploy','execute','resume-preparation','stream-evidence'))
    p.add_argument('--case',choices=('control','limited'));args=p.parse_args()
    if args.action=='init':initialize();return
    if args.action=='configure-limited':configure_limited();return
    if not args.case:raise ValueError('Case required')
    m=runner(args.case)
    if m.ssm.aws('sts','get-caller-identity')['Account']!='483175530259':raise RuntimeError('Wrong account')
    case=json.loads((m.EVIDENCE/'plan.json').read_text())['rates'][0]
    if args.action=='deploy':m.deploy()
    elif args.action in ('execute','resume-preparation'):
        ensure_e2e_prepared(args.case)
        if 'snapshotBytesPerSecond' not in case:raise RuntimeError('Dump rate not fixed before input')
        action=m.resume_preparation if args.action=='resume-preparation' else m.execute
        action(case,case['connectorIndex'])
    else:
        command="python3 - <<'READONLY'\nfrom pathlib import Path\nprint((Path('/run/yeodam-b391')/"+repr(case['runId'])+"/'snapshot-stream.json').read_text())\nREADONLY"
        result=m.records(m.raw('source',[command],bound=60))[-1]
        (m.EVIDENCE/'snapshot-stream.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))


if __name__=='__main__':main()
