#!/usr/bin/env python3
"""Compare the completed instrumented180s pair; never mix15min percentiles."""
import importlib.util
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location('batch472',ROOT/'scripts/migration-batch472.py')
a=importlib.util.module_from_spec(s);s.loader.exec_module(a)

def auth_failures(counts):
 return sum(value for label,value in counts.items() if label not in ('success','200'))

def measurements(directory):
 plan=json.loads((directory/'plan.json').read_text());run=plan['rates'][0]['runId']
 state=json.loads((directory/(run+'.state.json')).read_text())
 if state.get('phase')!='completed' or state.get('runtimeRestored') is not True:raise RuntimeError('Only completed restored diagnostics')
 analysis=json.loads((directory/'diagnostic-analysis.json').read_text())
 load=state['loadResult'];metrics=state['completionEvidence']['completionMetrics'];audit=state['sourceAudit']['all']
 dropped=load.get('native',{}).get('dropped_iterations',{}).get('count')
 rows=analysis['mysql']['all_input']['allChangedDigests']
 commit=[r for r in rows if r['sql'].strip().upper()=='COMMIT']
 if len(commit)!=1:raise RuntimeError('COMMIT digest ambiguous; do not guess')
 commit=commit[0]
 return {'runId':run,'inputSeconds':plan['durationSeconds'],'writerSwitch':plan['writerSwitch'],
  'started':load['started'],'accepted202':load['firstStatuses'].get('202',0),'k6ExitCode':load['k6ExitCode'],
  'dropped':dropped,'completed':metrics['count'],'sourceAuditRows':audit['rows'],'missingRegions':audit['missingRegions'],
  'projectionMatches':load['requestProjectionSha256']==audit['requestProjectionSha256'],
  'ackP95Ms':load['firstResponseMs']['p95'],'completionP95Ms':metrics['commandCreatedToCommitObservedMs']['p95'],
  'refreshFailures':auth_failures(load['refreshes']),
  'csrfFailures':auth_failures(load['csrf']),
  'commitCallsDuringInput':commit['calls'],'commitMeanElapsedMs':commit['meanElapsedMs'],
  'recordedJvmCpuFraction':sum(analysis['jfr']['app']['jvmCpuReportedMeans'][k] for k in ['jvmUser','jvmSystem']),
  'sqlCpuCounterUsable':analysis['mysql']['all_input']['cpuCounterNonzero']}

def main():
 control=measurements(a.CONTROL/a.CASE);candidate=measurements(a.BASE/a.CASE)
 for v in [control,candidate]:
  if v['inputSeconds']!=180 or v['writerSwitch'] is not False:raise RuntimeError('Only matched180s non-cutover diagnostics')
 integrity=(candidate['started']>0 and candidate['started']==candidate['accepted202']==candidate['completed']==candidate['sourceAuditRows']
  and candidate['k6ExitCode']==0 and candidate['dropped']==0 and candidate['missingRegions']==0
  and candidate['projectionMatches']
  and candidate['refreshFailures']==candidate['csrfFailures']==0)
 improvement=candidate['completionP95Ms']<control['completionP95Ms']
 out={'diagnosticRunId':candidate['runId'],'control':control,'candidate':candidate,
  'integrityPassed':integrity,'completionP95Lower':improvement,'eligibleForIntegration':integrity and improvement,
  'ackP95Higher':candidate['ackP95Ms']>control['ackP95Ms'],
  'completionP95ChangePercent':100*(candidate['completionP95Ms']/control['completionP95Ms']-1),
  'limitations':['One matched instrumented pair; no statistical significance claim.',
   'COMMIT count includes other transactions and elapsed time is not CPU time.',
   'The15min Writer/Redis/V1/V2E2E acceptance remains separate.']}
 (a.ISSUE/'diagnostic-benefit.json').write_text(json.dumps(out,indent=2));print(json.dumps(out,indent=2))

if __name__=='__main__':main()
