#!/usr/bin/env python3
"""Offline SQL-counter and safe JFR-summary analysis for diagnostic467."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

DOCS=Path(__file__).resolve().parents[2]/'KTB4-2nd-Cloud/.local-docs'
BASE=DOCS/'evidence/467-java-mysql/index50'
FIELDS=['COUNT_STAR','SUM_TIMER_WAIT','SUM_CPU_TIME','SUM_LOCK_TIME','SUM_ROWS_EXAMINED','SUM_ROWS_SENT']

def number(value):
 return None if value is None or str(value).upper() in ('','NULL','NONE') else int(value)

def category(sql):
 s=' '.join(sql.replace('`','').upper().split())
 if 'PERFORMANCE_SCHEMA' in s or 'INFORMATION_SCHEMA' in s or s.startswith('SHOW GLOBAL STATUS'):return 'observer_metadata'
 if s.startswith('SELECT') and ('SQL_NO_CACHE' in s or ('* FROM ' in s and ' WHERE ' not in s)):return 'snapshot_full_scan'
 if 'MIGRATION_TRIP_REQUEST_RESULT' in s:return 'trip_journal'
 if 'MIGRATION_AUTH_REQUEST_RESULT' in s:return 'auth_journal'
 if 'LOGIN_SESSIONS' in s or 'CSRF_TOKENS' in s:return 'authentication'
 if s.startswith(('COMMIT','ROLLBACK')):return 'transaction_end'
 if s.startswith('SET'):return 'session_control'
 if s.startswith('SELECT') and 'TRIP_NAME' in s:return 'trip_name_lookup'
 if s.startswith(('INSERT','UPDATE','DELETE')) and ('TRIPS' in s or 'TRIP_REGIONS' in s):return 'trip_write'
 return 'other'

def digest_delta(before,after):
 old={(v['SCHEMA_NAME'],v['DIGEST']):v for v in before['digests']};rows=[];resets=[]
 for v in after['digests']:
  key=(v['SCHEMA_NAME'],v['DIGEST']);previous=old.get(key,{})
  diffs={k:(None if number(v.get(k)) is None else number(v.get(k))-(number(previous.get(k)) or 0)) for k in FIELDS}
  if any(x is not None and x<0 for x in diffs.values()):resets.append(list(key));continue
  if not diffs.get('COUNT_STAR'):continue
  count=diffs['COUNT_STAR'];elapsed=(diffs['SUM_TIMER_WAIT'] or 0)/1e12;cpu=None if diffs['SUM_CPU_TIME'] is None else diffs['SUM_CPU_TIME']/1e12
  rows.append({'schema':key[0],'digest':key[1],'sql':v['DIGEST_TEXT'],'category':category(v['DIGEST_TEXT']),'calls':count,'elapsedSeconds':elapsed,'meanElapsedMs':elapsed*1000/count,'cpuSeconds':cpu,'lockSeconds':(diffs['SUM_LOCK_TIME'] or 0)/1e12,'rowsExamined':diffs['SUM_ROWS_EXAMINED']})
 vanished=[list(k) for k in old if k not in {(v['SCHEMA_NAME'],v['DIGEST']) for v in after['digests']}]
 groups={}
 for r in rows:
  g=groups.setdefault(r['category'],{'calls':0,'elapsedSeconds':0.0,'cpuSeconds':0.0,'rowsExamined':0})
  for k in g:g[k]+=r.get(k) or 0
 observed=any((r['cpuSeconds'] or 0)>0 for r in rows)
 for g in groups.values():g['meanElapsedMs']=g['elapsedSeconds']*1000/g['calls']
 return {'checkpointSeconds':[before['startedAt'],after['startedAt']],'counterResetDigests':resets,'vanishedDigests':vanished,'cpuCounterNonzero':observed,'categories':groups,'topByElapsed':sorted(rows,key=lambda r:r['elapsedSeconds'],reverse=True)[:30],'topByCpu':sorted(rows,key=lambda r:r['cpuSeconds'] or 0,reverse=True)[:30] if observed else [],'allChangedDigests':rows}

def summary_delta(before,after,field):
 old={v[0]:v[1:] for v in before[field]};result=[]
 for row in after[field]:
  previous=old.get(row[0]);diff=[]
  for i,value in enumerate(row[1:]):
   n=number(value);p=number(previous[i]) if previous else 0;diff.append(None if n is None else n-(p or 0))
  if any(x is not None and x<0 for x in diff):result.append({'event':row[0],'counterReset':True});continue
  if any(diff):result.append({'event':row[0],'delta':diff})
 return result

def main():
 p=argparse.ArgumentParser();p.add_argument('--pull',action='store_true');a=p.parse_args();plan=json.loads((BASE/'plan.json').read_text());run=plan['rates'][0]['runId'];raw=BASE/'raw';raw.mkdir(exist_ok=True)
 files=['resource-samples.jsonl','completions.json','snapshot-stream.json','app-jfr-analysis.json','consumer-jfr-analysis.json']+['mysql-'+n+'.json' for n in ['before-input','snapshot-start','snapshot-end','input-end']]
 if a.pull:
  for name in files:
   dest=raw/name
   if dest.exists():raise RuntimeError('Keep already downloaded raw evidence')
   subprocess.run(['aws','s3','cp','s3://yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2/migration/performance455/diagnostics467/'+run+'/'+name,str(dest),'--profile','yeodam-admin','--region','ap-northeast-2','--only-show-errors'],check=True)
 c={n:json.loads((raw/('mysql-'+n+'.json')).read_text()) for n in ['before-input','snapshot-start','snapshot-end','input-end']}
 windows={}
 for label,b,e in [('before_snapshot','before-input','snapshot-start'),('snapshot','snapshot-start','snapshot-end'),('after_snapshot','snapshot-end','input-end'),('all_input','before-input','input-end')]:
  if c[b]['startedAt']>=c[e]['startedAt']:raise RuntimeError('Checkpoint order differs; no fabricated phase analysis')
  d=digest_delta(c[b],c[e]);d['waits']=summary_delta(c[b],c[e],'waits');d['files']=summary_delta(c[b],c[e],'files');d['statementEvents']=summary_delta(c[b],c[e],'statementEvents');windows[label]=d
 state=json.loads((BASE/(run+'.state.json')).read_text());jr={role:json.loads((raw/(role+'-jfr-analysis.json')).read_text()) for role in ['app','consumer']}
 if any(v['deniedEventCounts'] for v in jr.values()):raise RuntimeError('Sensitive event present; do not publish')
 result={'runId':run,'diagnosticOnly':True,'writerSwitched':False,'inputSeconds':180,'fixedSettingsFrom':'463 successful50;180s input and observation only','jfr':jr,'mysql':windows,'load':{k:state['loadResult'].get(k) for k in ['started','firstStatuses','k6ExitCode','firstResponseMs','refreshes','csrf','native']},'completionEvidence':state['completionEvidence'],'sourceAudit':state['sourceAudit'],'counterUnits':{'elapsedAndCpu':'seconds; SUM_TIMER_WAIT/SUM_CPU_TIME are picoseconds in raw','waitDelta':'raw count,picoseconds; concurrent totals are not CPU time','fileDelta':'raw read count/time,write count/time,misc count/time; misc is not synonymous with fsync'},'limitations':['Instrumented short diagnosis, not a replacement migration acceptance or performance improvement comparison.','Cumulative summaries include observed system/background work and collection overhead; no counter resets.','JFR sampling and thresholds cannot identify every SQL wait; absent event is not zero cost.']}
 (BASE/'diagnostic-analysis.json').write_text(json.dumps(result,indent=2));(BASE/'raw-manifest.json').write_text(json.dumps({f.name:{'bytes':f.stat().st_size,'sha256':hashlib.sha256(f.read_bytes()).hexdigest()} for f in raw.iterdir()},indent=2))
 for label,w in windows.items():print(json.dumps({'window':label,'cpuNonzero':w['cpuCounterNonzero'],'categories':w['categories'],'resets':len(w['counterResetDigests'])}))
 for role,j in jr.items():print(json.dumps({'role':role,'samples':j['executionSamples'],'cpu':j['jvmCpuReportedMeans'],'hotLeaf':list(j['topLeafSamples'].items())[:8],'durations':j['recordedDurationSumMs']}))

if __name__=='__main__':main()
