#!/usr/bin/env python3
"""Reuse the fixed baseline and B migration runners with identical read probes."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import uuid

ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=ROOT.parent/'KTB4-2nd-Cloud/.local-docs/evidence/406-read-protection'
# The actual immutable image is shared with #398/#404, not a new BE build.
IMAGE='ghcr.io/100-hours-a-week/yeodam-backend@sha256:89b24d02225c8fb6143a4ee323e9b3221c18676c0ab14056b38a6edc608ad4e1'

def baseline():
    spec=importlib.util.spec_from_file_location('baseline406',ROOT/'scripts/run-b404-source-baseline.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    m.EVIDENCE=EVIDENCE/'baseline';m.b.EVIDENCE=m.b.ssm.EVIDENCE=m.EVIDENCE
    return m

def migration():
    spec=importlib.util.spec_from_file_location('migration406',ROOT/'scripts/b-integration376-run.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    m.EVIDENCE=m.ssm.EVIDENCE=EVIDENCE/'migration'
    return m

def initialize():
    if (EVIDENCE/'contract.json').exists():raise RuntimeError('Comparison already initialized; preserve UUIDs')
    for name in ('baseline','migration'):(EVIDENCE/name).mkdir(parents=True,exist_ok=True)
    # Reference contains only logical-user IDs, not session credentials.
    shutil.copy(EVIDENCE.parent/'404-source-baseline/reference.json',EVIDENCE/'baseline/reference.json')
    (EVIDENCE/'baseline/enable-read-probes').touch()
    case={'rate':30,'runId':str(uuid.uuid4()),'candidate':'finite-watermark','readProbes':True,'connectorIndex':406}
    (EVIDENCE/'migration/plan.json').write_text(json.dumps({'backendImage':IMAGE,'rates':[case]},indent=2))
    value={'writeRate':30,'readListRate':1,'readDetailRate':1,'duration':900,'fixedCheckpoint':1020,
           'sourceConcurrency':4,'targetConcurrency':8,'batch':32,'readUsers':20,
           'latestFenceSeconds':360,'cdcBudgetSeconds':45,'activationBudgetSeconds':50,
           'backendImage':IMAGE,'migrationRunId':case['runId'],'dataAndCachesNotReset':True}
    (EVIDENCE/'contract.json').write_text(json.dumps(value,indent=2));print(json.dumps(value))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('init','baseline-deploy','baseline-start','baseline-observe','baseline-finish','migration-deploy','migration-execute'));a=p.parse_args()
    if a.action=='init':initialize()
    elif a.action.startswith('baseline-'):
        m=baseline()
        if m.b.ssm.aws('sts','get-caller-identity')['Account']!='483175530259':raise RuntimeError('Wrong account')
        getattr(m,a.action.split('-',1)[1])()
    else:
        m=migration()
        if m.ssm.aws('sts','get-caller-identity')['Account']!='483175530259':raise RuntimeError('Wrong account')
        if a.action=='migration-deploy':m.deploy()
        else:
            s=json.loads((EVIDENCE/'baseline/state.json').read_text())
            if s['phase']!='completed':raise RuntimeError('Baseline not finalized')
            case=json.loads((m.EVIDENCE/'plan.json').read_text())['rates'][0]
            m.execute(case,case['connectorIndex'])
