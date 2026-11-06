#!/usr/bin/env python3
"""Isolated synthetic MySQL pair only. Never uses AWS, kubeconfig or real credentials."""
import argparse,importlib.util,json,os,secrets,shutil,subprocess,tempfile,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def module(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
cdc=module('cdc',ROOT/'scripts/manage-v3-mysql-cdc.py');bootstrap=module('bootstrap',ROOT/'scripts/create-v3-mysql-bootstrap-secret.py')
MYSQL='public.ecr.aws/docker/library/mysql@sha256:a81d043930e51745522bd5c430961cf8286f0a23f0a9a2a6e0108f7ea71b0902'

def smoke(image,output):
 output.mkdir(parents=True,exist_ok=True);prefix='yeodam-schema-'+uuid.uuid4().hex[:10];net=prefix+'-net';containers=[];d=Path(tempfile.mkdtemp(prefix=prefix));d.chmod(0o755);counter=0
 def command(args,source=None,check=True,timeout=120):
  nonlocal counter
  r=subprocess.run(list(map(str,args)),input=source,capture_output=True,text=True,timeout=timeout);counter+=1
  (output/f'command-{counter}.log').write_text(r.stdout+r.stderr)
  if check and r.returncode:raise RuntimeError('Synthetic schema check failed; diagnostic command '+str(counter))
  return r
 def docker(*args,**kw):return command(['docker',*args],**kw)
 def sql(member,query,check=True):return docker('exec','-i',prefix+'-'+member,'/bin/bash','-ec',cdc.ROOT_SHELL,source=query,check=check).stdout.strip()
 def wait(member,query,expected):
  for _ in range(90):
   state=json.loads(docker('inspect',prefix+'-'+member).stdout)[0]['State']
   if not state['Running']:raise RuntimeError('Synthetic MySQL exited; see retained fixture logs')
   if sql(member,query,False)==expected:return
   time.sleep(2)
  raise RuntimeError('Synthetic MySQL did not reach expected state')
 try:
  cred=d/'credentials';cred.mkdir();cred.chmod(0o755)
  p=bootstrap.create_payload(cred);cred.chmod(0o755) # Only disposable synthetic credentials; container UIDs must traverse.
  # Public synthetic CA/private fixtures exist only on this test's network and are removed.
  for key,name in [('rootPassword','root'),('adminPassword','admin'),('appPassword','app'),('replicationPassword','replication'),('healthPassword','health')]:
   (cred/(name+'-password')).write_text(p[key])
  for f in cred.iterdir():f.chmod(0o444)
  docker('network','create','--internal','--label','yeodam.issue=540',net)
  for i in range(2):
   data=d/f'data{i}';data.mkdir();data.chmod(0o777);name=prefix+f'-mysql-{i}';containers.append(name)
   aliases=['--network-alias',f'mysql-{i}.mysql-members.mysql.svc.cluster.local']
   if i==0:aliases+=['--network-alias','mysql-primary.mysql.svc.cluster.local']
   docker('run','-d','--name',name,'--label','yeodam.issue=540','--platform','linux/amd64','--network',net,*aliases,'--memory','768m','--cpus','1','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--user','999:999','--tmpfs','/tmp:rw,size=128m','-v',f'{ROOT}/deploy/v3/mysql/files:/runtime:ro','-v',f'{cred}:/credentials:ro','-v',f'{cred}:/tls:ro','-v',f'{data}:/var/lib/mysql','-e',f'POD_NAME=mysql-{i}','-e','PRIMARY_MEMBER=mysql-0','--entrypoint','/bin/bash',MYSQL,'/runtime/entrypoint.sh')
  wait('mysql-0','SELECT 1;','1');wait('mysql-1','SELECT 1;','1')
  docker('exec',prefix+'-mysql-0','/bin/bash','/runtime/bootstrap.sh',timeout=240)
  print('Synthetic MySQL GTID/TLS/semi-sync pair ready',flush=True)
  def migrate(check=True,tls=cred):
   return docker('run','--rm','--platform','linux/amd64','--network',net,'--memory','512m','--cpus','.5','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--tmpfs','/tmp:rw,size=32m','-v',f'{cred}:/credentials:ro','-v',f'{tls}:/tls:ro',image,check=check,timeout=240)
  migrate();history="SELECT CONCAT(COUNT(*),':',MAX(CAST(version AS UNSIGNED))) FROM yeodam.flyway_schema_history WHERE success=1;"
  assert sql('mysql-0',history)=='34:34';wait('mysql-1',history,'34:34');migrate();assert sql('mysql-0',history)=='34:34'
  checksum=sql('mysql-0',"SELECT checksum FROM yeodam.flyway_schema_history WHERE version='1';")
  sql('mysql-0',"UPDATE yeodam.flyway_schema_history SET checksum=checksum+1 WHERE version='1';")
  assert migrate(False).returncode!=0,'Checksum mismatch was accepted'
  assert sql('mysql-0',history)=='34:34'
  sql('mysql-0',f"UPDATE yeodam.flyway_schema_history SET checksum={int(checksum)} WHERE version='1';")
  wrong=d/'wrong-ca';wrong.mkdir();wrong.chmod(0o755)
  command(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=untrusted-fixture-ca','-keyout',str(d/'wrong.key'),'-out',str(wrong/'ca.crt')])
  (wrong/'ca.crt').chmod(0o444)
  assert migrate(False,wrong).returncode!=0,'Wrong CA was accepted'
  assert sql('mysql-0',history)=='34:34'
  password=secrets.token_hex(24);sql('mysql-0',cdc.create_sql(password));wait('mysql-1',f"SELECT COUNT(*) FROM mysql.user WHERE user='{cdc.USER}';",'1')
  for member in ['mysql-0','mysql-1']:
   assert cdc.grants_match(sql(member,f"SHOW GRANTS FOR '{cdc.USER}'@'%';"))
   r=docker('exec','-i',prefix+'-'+member,'/bin/bash','-ec',cdc.AUTH_SHELL,source=password+'\n');assert 'Ssl_cipher\tTLS' in r.stdout
   assert sql(member,f"SELECT COUNT(*) FROM mysql.user WHERE user='{cdc.USER}' AND ssl_type='ANY' AND max_user_connections=4;")=='1'
  assert sql('mysql-0',"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='yeodam' AND table_name='users';")=='1'
  # Exercise allowed and denied statements with the actual CDC identity over verified TLS.
  auth="""set -Eeuo pipefail
read -r p
f=/tmp/cdc-negative-$$.cnf
trap 'rm -f "$f"' EXIT
printf '[client]\\nuser=v3_connect_cdc\\npassword=%s\\nhost=mysql-primary.mysql.svc.cluster.local\\nssl-mode=VERIFY_IDENTITY\\nssl-ca=/tls/ca.crt\\n' "$p" > "$f"
mysql --defaults-extra-file="$f" --silent -e 'SELECT COUNT(*) FROM yeodam.trip_creation_outbox;'
if mysql --defaults-extra-file="$f" --silent -e 'SELECT * FROM yeodam.users LIMIT 0;' 2>/dev/null; then exit 1; fi
if mysql --defaults-extra-file="$f" --silent -e 'DELETE FROM yeodam.trip_creation_outbox WHERE 1=0;' 2>/dev/null; then exit 1; fi
if mysql --defaults-extra-file="$f" --silent -e 'CREATE TABLE yeodam.cdc_forbidden(id INT);' 2>/dev/null; then exit 1; fi
if mysql --defaults-extra-file="$f" --ssl-mode=DISABLED --silent -e 'SELECT 1;' 2>/dev/null; then exit 1; fi
echo 'PASS: exact Outbox SELECT and denial of unrelated reads, writes, DDL and non-TLS'
"""
  docker('exec','-i',prefix+'-mysql-0','/bin/bash','-ec',auth,source=password+'\n')
  assert sql('mysql-0',"SELECT @@global.read_only;")=='0' and sql('mysql-1',"SELECT @@global.super_read_only;")=='1'
  (output/'result.json').write_text(json.dumps({'migrations':34,'flywayVersion':'12.4.0','rerunNoop':True,'checksumMismatchRejected':True,'tlsIdentityVerified':True,'wrongCaRejected':True,'externalNetworkUnavailable':True,'replicaHistoryMatches':True,'cdcExactGrantsAndTls':True,'cdcDeniedOtherReadWriteDdlPlaintext':True,'flywayMemoryLimitMi':512,'flywayCpuLimit':0.5},indent=2))
  print('PASS: BE V1-V34, no-op retry, checksum rejection, replica history, CDC least privileges and TLS negatives',flush=True)
 finally:
  for name in reversed(containers):
   try:
    r=docker('logs',name,check=False);(output/(name+'.log')).write_text(r.stdout+r.stderr);docker('rm','-f',name,check=False)
   except Exception:pass
  docker('network','rm',net,check=False)
  # Linux bind data belongs to MySQL UID999. Remove only our fixture data as that UID.
  for data in [d/'data0',d/'data1']:
   if data.exists():
    docker('run','--rm','--platform','linux/amd64','--network','none','--read-only','--cap-drop','ALL','--user','999:999','-v',f'{data}:/cleanup','--entrypoint','/bin/bash',MYSQL,'-ec','rm -rf /cleanup/* /cleanup/.[!.]* /cleanup/..?*',check=False)
  shutil.rmtree(d)
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--image',required=True);p.add_argument('--output',required=True,type=Path);a=p.parse_args();smoke(a.image,a.output)
