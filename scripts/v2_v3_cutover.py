"""Cutover contracts and receipts. Default invocation never changes a live resource."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,json,os,re,subprocess
from pathlib import Path
import v2_v3_mysql_logical as mysql
import qdrant_migration as qdrant
ROOT=Path(__file__).resolve().parents[1]
POLICY=json.loads((ROOT/'deploy/v3/cutover/policy.json').read_text())
KAFKA=json.loads((ROOT/'deploy/v3/kafka-access/files/business-contract.json').read_text())
GROUPS={g for role,a in KAFKA['users'].items() if role!='connect-outbox' for g in a['groups']}
UNITS=json.loads((ROOT/'deploy/v3/releases/units.json').read_text())
PHASES=POLICY['phases']
require_group_bindings=set(POLICY['consumerTopics'])==GROUPS
if not require_group_bindings:raise ValueError('Every literal app consumer group must have reviewed topic bindings')
require=mysql.require
canonical=qdrant.canonical
def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def tool_commit():return subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
def now():return datetime.now(timezone.utc).isoformat()
def write(path,value):mysql.private_json(path,value)
def load(path):
 require(path.is_file() and not path.is_symlink() and path.stat().st_size<16*1024*1024,'Invalid private evidence file')
 return json.loads(path.read_text())
def audit_jobs(execute):
 """No business rows leave the query; terminal failures stay failures during migration."""
 tables=execute("SELECT table_name FROM information_schema.tables WHERE table_schema='yeodam' AND table_type='BASE TABLE' ORDER BY table_name;").splitlines()
 result={}
 for table in tables:
  if not (table.endswith(('_jobs','_executions','_outbox'))):continue
  mysql.identifier(table)
  columns=execute("SELECT column_name FROM information_schema.columns WHERE table_schema='yeodam' AND table_name='"+table+"';").splitlines()
  require('status' in columns,'Unreviewed job status column')
  statuses=execute('SELECT status,COUNT(*) FROM yeodam.'+mysql.identifier(table)+' GROUP BY status ORDER BY status;').splitlines()
  counts={}
  for row in statuses:
   state,count=row.split('\t');require(re.fullmatch('[A-Z_]+',state) and count.isdigit(),'Invalid aggregate job evidence');counts[state]=int(count)
  terminal={'COMPLETED','SUCCEEDED','FAILED','CANCELED','CANCELLED','EXPIRED','PUBLISHED'}
  result[table]={'statuses':counts,'unresolved':sum(n for state,n in counts.items() if state not in terminal)}
 require({'trip_creation_executions','story_generation_jobs','trip_creation_outbox'}<=set(result),'Incomplete source job inventory')
 return result

def validate_reconciliation(review):
 require(set(review)=={'capturedAt','sourceJobs','qaJobs','qaConsumerLag','qaAiQueueItems','qaUploadsInFlight','sourceWritersStopped','qaAccepted','photoObjectsVerified','sourceQdrantMatchesBackup'},'Exact reconciliation fields required')
 age=(datetime.now(timezone.utc)-datetime.fromisoformat(review['capturedAt'])).total_seconds()
 require(0<=age<=3600,'Reconciliation must be less than one hour old')
 require(type(review['photoObjectsVerified']) is int and review['photoObjectsVerified']>=0 and review['sourceQdrantMatchesBackup'] is True,'Fresh source vector and copied-photo proof required')
 require(review['qaAccepted'] is True and review['sourceWritersStopped'] is True,'QA completion and stopped V2 writers required')
 for field in ['sourceJobs','qaJobs']:
  require(isinstance(review[field],dict) and {'trip_creation_executions','story_generation_jobs','trip_creation_outbox'}<=set(review[field]),'Incomplete job aggregates')
  require(all(type(x.get('unresolved')) is int and x['unresolved']==0 for x in review[field].values()),'Unresolved business jobs; never cancel or replay automatically')
 require(type(review['qaConsumerLag']) is dict and set(review['qaConsumerLag'])==GROUPS and all(type(n) is int and n==0 for n in review['qaConsumerLag'].values()),'All reviewed QA consumer groups must be at committed end offsets')
 for key in ['qaAiQueueItems','qaUploadsInFlight']:require(type(review[key]) is int and review[key]==0,'QA work still in flight')

def plan(mysql_path,qdrant_path,photos_path,reconciliation):
 m=mysql.verify_package(mysql_path);q=qdrant.verify_package(qdrant_path);photos=load(photos_path)
 require(m['sensitivity']==q['sensitivity']=='private-user-data' and m.get('sourceInstance')==POLICY['sourceMysql'],'Actual fixed-source backups required')
 require(photos.get('sourceBucket')=='yeodam-v1-app-data-483175530259-ap-northeast-2' and photos.get('targetBucket')=='yeodam-v3-foundation-app-data-483175530259-ap-northeast-2' and all(photos.get(k)==0 for k in ['failedObjects','missingSource','targetConflicts','existingObjectsOverwritten','objectsDeleted']) and photos.get('allCopiedVersionsVerified') is True,'Photo copy must be fully verified with no failures/conflicts')
 validate_reconciliation(reconciliation)
 require(reconciliation['photoObjectsVerified']==photos['copiedObjects'],'Copied-photo inventory differs from fresh verification')
 data={'version':1,'toolCommit':tool_commit(),'candidateSha256':digest(load(ROOT/'deploy/v3/releases/candidate.json')),'policySha256':digest(POLICY),'cluster':POLICY['cluster'],'domain':POLICY['domain'],
  'createdAt':now(),'mysqlPackage':str(mysql_path.resolve()),'mysqlSha256':m['sha256'],
  'qdrantPackage':str(qdrant_path.resolve()),'qdrantSha256':q['snapshotSha256'],
  'photosReceipt':str(photos_path.resolve()),'photosSha256':digest(photos),'reconciliation':reconciliation,
  'phases':PHASES,'awsCreates':0,'gpuChanges':0,'v2DeletionEnabled':False}
 return {**data,'planSha256':digest(data)}

def verify_plan(p,approved):
 value=dict(p);sha=value.pop('planSha256',None)
 require(sha==approved==digest(value) and p['policySha256']==digest(POLICY),'Plan changed; obtain a new exact approval')
 require(p['toolCommit']==tool_commit() and p['candidateSha256']==digest(load(ROOT/'deploy/v3/releases/candidate.json')),'Cutover code/candidate changed; review a fresh plan')
 require(p['cluster']==POLICY['cluster'] and p['domain']==POLICY['domain'] and p['phases']==PHASES,'Wrong target')
 require(p['awsCreates']==p['gpuChanges']==0 and p['v2DeletionEnabled'] is False,'No creation or V2 deletion in cutover runtime')
 require(not os.environ.get('GITHUB_ACTIONS'),'Live cutover is forbidden in CI')
 m=mysql.verify_package(Path(p['mysqlPackage']));q=qdrant.verify_package(Path(p['qdrantPackage']))
 require(m['sha256']==p['mysqlSha256'] and q['snapshotSha256']==p['qdrantSha256'] and digest(load(Path(p['photosReceipt'])))==p['photosSha256'],'Approved backup bytes changed')
 return sha

def prior_receipts(directory,phase,sha):
 require(phase in PHASES,'Unknown cutover phase')
 previous=None
 for name in PHASES[:PHASES.index(phase)]:
  receipt=load(directory/(name+'.json'))
  require(receipt.get('phase')==name and receipt.get('planSha256')==sha and receipt.get('completed') is True and receipt.get('previous')==previous,'Missing or broken prior phase receipt')
  previous=digest(receipt)
 require(not (directory/(phase+'.json')).exists(),'Phase already completed; do not repeat writes')
 return previous

def receipt(directory,phase,sha,previous,result):
 require(result.get('verified') is True,'Do not record an unverified phase as complete')
 value={'phase':phase,'planSha256':sha,'previous':previous,'completed':True,'completedAt':now(),'result':result}
 write(directory/(phase+'.json'),value);return value

def operating_candidate(candidate):
 value=deepcopy(candidate);origin='https://'+POLICY['domain']
 fe=value['values']['frontend'];fe['defaults']['configMapData']['SERVER_API_BASE_URL']=origin+'/api'
 for role,config in value['values']['backend']['workloads'].items():
  data=config.setdefault('configMapData',{})
  data.update(FRONTEND_ORIGIN=origin,FRONTEND_OAUTH_CALLBACK_URI=origin+'/auth/callback',KAKAO_REDIRECT_URI=origin+'/api/auth/kakao/callback')
 value['values']['backend']['defaults']['configMapData'].update(FRONTEND_ORIGIN=origin,FRONTEND_OAUTH_CALLBACK_URI=origin+'/auth/callback',KAKAO_REDIRECT_URI=origin+'/api/auth/kakao/callback')
 value['values']['websocket']['defaults']['configMapData']['WEBSOCKET_ALLOWED_ORIGINS']=origin
 for config in value['values']['websocket']['workloads'].values():config.setdefault('configMapData',{})['WEBSOCKET_ALLOWED_ORIGINS']=origin
 require(value['components']==candidate['components'],'Cutover must preserve all QA image digests')
 return value

def operating_cdc(config):
 value=deepcopy(config);value['topic.prefix']=POLICY['cdcPrefix'];value['schema.history.internal.kafka.topic']=POLICY['cdcHistory']
 # Preserve payload wire topics and use a different source-partition key, never DELETE offsets.
 value['predicates.heartbeat.pattern']='^__debezium-heartbeat\\.v3\\.yeodam\\.operating\\.outbox$'
 require(value['snapshot.mode']=='initial','One initial snapshot must read only pending owned rows')
 return value


def verify_photos(plan,results,store,backup_sha):
 from concurrent.futures import ThreadPoolExecutor
 import v2_v3_photo_migration as photos
 require(plan['ready'] is True and plan['backupSha256']==backup_sha and results['completed'] is True and results['planSha256']==photos.digest(plan),'Photo proof differs from the approved source backup/copy')
 require(plan.get('sqlSha256')==photos.SQL_SHA256,'Photo references differ from the reviewed SQL')
 require(plan['sourceBucket']==photos.SOURCE and plan['targetBucket']==photos.TARGET,'Wrong photo buckets')
 versions={r['key']:r['versionId'] for r in results['results'] if r['status'] in ['copied','reused']}
 require(set(versions)=={o['key'] for o in plan['objects']},'Incomplete copied-photo versions')
 store.assert_buckets()
 def inspect(obj):
  current=store.observe(photos.TARGET,obj['key'])
  require(current is not None and current['head']['VersionId']==versions[obj['key']] and photos.equal(obj['source'],current),'A copied photo changed or disappeared; no overwrite is allowed')
 with ThreadPoolExecutor(max_workers=4) as executor:list(executor.map(inspect,plan['objects']))
 return len(versions)
