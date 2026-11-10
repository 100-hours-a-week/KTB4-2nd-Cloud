"""Fixed, deletion-only V2 retirement contracts. Never prints State, plan values or secrets."""
from pathlib import Path
import hashlib,json,os
ROOT=Path(__file__).resolve().parents[1]
POLICY=json.loads((ROOT/'deploy/v2/retirement/policy.json').read_text())
ASSOCIATION='aws_route53_zone_association.legacy_production["production"]'
INSTANCES={'module.mysql.aws_instance.mysql':'i-02b2db8886a13dc25','aws_instance.kafka':'i-00aaea122d7a2420b','aws_instance.ai_redis':'i-0d706c88c31f425a5','aws_instance.plg':'i-015e8dd17ade9e192'}

def require(ok,message):
 if not ok:raise ValueError(message)
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
def file_sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def load(path):
 require(path.is_file() and not path.is_symlink() and path.stat().st_size<64*1024*1024,'Regular bounded evidence file required')
 return json.loads(path.read_text())
def write(path,value):
 path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:json.dump(value,f,indent=2)
def source_hash():
 paths=list((ROOT/'terraform/v2/retirement').rglob('*.tf'))+list((ROOT/'terraform/v2/retirement').rglob('*.tf.json'))+list((ROOT/'terraform/v2/retirement').rglob('.terraform.lock.hcl'))
 paths += [ROOT/p for p in ['scripts/v2_retirement.py','scripts/retire-v2-resources.py','deploy/v2/retirement/policy.json','terraform/retirement-guard.tf','terraform/v2/production/retirement-guard.tf','scripts/finalize-v2-v3-cutover.py','scripts/run-v2-v3-cutover.py','scripts/v2_v3_cutover.py','scripts/qdrant_migration.py','scripts/v2_v3_mysql_logical.py','deploy/v3/releases/candidate.json','deploy/v3/releases/units.json','deploy/v3/cutover/policy.json']]
 return digest({str(p.relative_to(ROOT)):file_sha(p) for p in sorted(paths) if '.terraform' not in p.parts})
def provider_valid(plan):
 configs=plan.get('configuration',{}).get('provider_config',{})
 aws=[p for p in configs.values() if p.get('full_name')=='registry.terraform.io/hashicorp/aws']
 require(len(aws)==1,'One fixed AWS provider required')
 expr=aws[0].get('expressions',{})
 require(expr.get('region',{}).get('constant_value')==POLICY['region'] and expr.get('allowed_account_ids',{}).get('constant_value')==[POLICY['account']],'Wrong AWS account/region constraint')
def inspect_plan(plan,phase,draft=False):
 require(phase in ['association','production','base'],'Unknown retirement phase');provider_valid(plan)
 scope=POLICY['states']['production' if phase=='association' else phase];retained=set(scope['retained']);allowed=set(scope['deletable']);deleted=[];imports=[];types={}
 protected_ids={str(x) for state in POLICY['states'].values() for x in state['retainedIds'].values()}|{POLICY['gpuInstance'],POLICY['gpuVpc'],POLICY['v3Vpc'],POLICY['gpuSecurityGroup']}
 old_sgs={v for a,v in scope['expectedIds'].items() if a.startswith('aws_security_group.')}
 for r in plan.get('resource_changes',[]):
  if r.get('mode')!='managed':continue
  a=r['address'];change=r['change'];actions=change['actions'];importing=change.get('importing') or r.get('importing')
  if importing:
   require(phase=='association' and a==ASSOCIATION and actions==['no-op'],'Only adopt the exact existing legacy VPC association')
   require(importing.get('id')==POLICY['privateZone']+':'+POLICY['retiredVpc'],'Wrong association import identity')
   after=change.get('after') or {};require(after.get('zone_id')==POLICY['privateZone'] and after.get('vpc_id')==POLICY['retiredVpc'],'Do not import a GPU/V3 association');imports.append(a)
  if actions==['no-op']:
   require(a in retained or (phase=='association' and a==ASSOCIATION),'Unexpected managed resource in retained configuration')
   if a in retained:require((change.get('before') or {}).get('id')==scope['retainedIds'][a],'Retained identity changed')
   continue
  require(phase!='association' and not importing,'Association adoption must not mutate AWS resources')
  require(actions==['delete'] and a in allowed and a not in retained,'Only exact allowlisted deletions; no create/update/replace')
  before=change.get('before') or {};typ=r['type'];identity=before.get('id')
  require((before.get('tags') or {}).get('Environment')!='staging','Never delete staging/shared GPU resources')
  require(str(identity) not in protected_ids,'Protected resource selected for deletion')
  if a in scope['expectedIds']:require(identity==scope['expectedIds'][a],'Resource identity differs from reviewed inventory')
  require(not typ.startswith('aws_s3_') and typ not in ['aws_iam_openid_connect_provider','aws_route53_zone'],'Never delete data buckets, OIDC or shared DNS zones')
  if typ=='aws_instance':require(a in INSTANCES and identity==INSTANCES[a],'Only four fixed V2 instances may terminate')
  if typ=='aws_vpc':require(identity==POLICY['retiredVpc'],'Only the retired production VPC may be deleted')
  if before.get('vpc_id'):require(before['vpc_id']==POLICY['retiredVpc'],'Resource is outside the retired VPC')
  if typ=='aws_vpc_peering_connection':require({before.get('vpc_id'),before.get('peer_vpc_id')}=={POLICY['retiredVpc'],POLICY['gpuVpc']},'Never remove the V3 GPU peering')
  if typ in ['aws_vpc_security_group_ingress_rule','aws_vpc_security_group_egress_rule'] and before.get('security_group_id')==POLICY['gpuSecurityGroup']:
   require(before.get('referenced_security_group_id') in old_sgs,'GPU rule must reference only a retired V2 SG')
  if typ=='aws_route' and before.get('route_table_id')=='rtb-0fc40e73348be69f0':require(before.get('destination_cidr_block')=='10.20.10.0/24','Never remove the GPU return route to V3')
  if typ=='aws_route53_zone_association':require(a==ASSOCIATION and before.get('zone_id')==POLICY['privateZone'] and before.get('vpc_id')==POLICY['retiredVpc'],'Wrong DNS association removal')
  if typ=='aws_secretsmanager_secret':require(before.get('recovery_window_in_days',0)>=7,'Secret recovery window must remain enabled')
  if typ=='aws_secretsmanager_secret_version':require(before.get('secret_id') in {v for n,v in scope['expectedIds'].items() if '.aws_secretsmanager_secret.' in '.'+n},'Do not remove an unknown/shared secret version')
  deleted.append(a);types[typ]=types.get(typ,0)+1
 if phase=='association':require(imports==[ASSOCIATION] and not deleted,'Exactly one state-only import required')
 else:
  require(not imports and deleted,'A nonempty deletion-only plan is required')
  if phase=='production' and not draft:require(ASSOCIATION in deleted,'Adopt the obsolete DNS association before final production Plan')
 return {'phase':phase,'draft':draft,'deletions':len(deleted),'imports':len(imports),'creates':0,'updates':0,'retained':len(retained),'byType':types,'deletedAddresses':sorted(deleted)}
def cutover_evidence(directory,complete=False):
 # The old tool commit remains immutable. Verify its completed evidence chain,
 # without rewriting it to the new retirement implementation's Git commit.
 plan=load(directory/'plan-recovery.json');p=dict(plan);sha=p.pop('planSha256');require(digest(p)==sha,'Cutover Plan hash differs')
 phases=json.loads((ROOT/'deploy/v3/cutover/policy.json').read_text())['phases'];previous=None
 selected=phases if complete else phases[:phases.index('accept')]
 last=None
 for phase in selected:
  receipt=load(directory/'run'/(phase+'.json'));require(receipt['phase']==phase and receipt['completed'] is True and receipt['planSha256']==sha and receipt['previous']==previous and receipt['result']['verified'] is True,'Broken cutover completion evidence')
  previous=digest(receipt);last=receipt
 require(load(directory/'run/dns.json')['result'].get('operatingAliasV3') is True,'Operating DNS cutover is not complete')
 if complete:
  checks=last['result']['functionalChecks'];require(set(checks)=={'login','existingTrips','existingPhotos','newTripCompleted','websocket','v2StillStopped'} and all(x is True for x in checks.values()),'Actual operating acceptance required before deletion')
 return {'cutoverPlanSha256':sha,'lastReceiptSha256':previous,'operatingRevision':load(directory/'run/dns.json')['result']['approvedRevision'],'acceptanceComplete':complete}
