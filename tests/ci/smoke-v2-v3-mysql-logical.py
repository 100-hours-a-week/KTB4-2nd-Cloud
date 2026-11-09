#!/usr/bin/env python3
"""Synthetic V32/9.7.2 -> logical restore -> V35/8.4.13. No AWS access."""
import argparse, importlib.util, json, shutil, subprocess, tempfile, time, uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def module(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
logical=module('logical',ROOT/'scripts/v2_v3_mysql_logical.py')
bootstrap=module('bootstrap',ROOT/'scripts/create-v3-mysql-bootstrap-secret.py')
def smoke(image,output):
 output.mkdir(parents=True,exist_ok=True);prefix='v2-v3-restore-'+uuid.uuid4().hex[:10]
 directory=Path(tempfile.mkdtemp(prefix=prefix));directory.chmod(0o755);containers=[];networks=[]
 def docker(*args,source=None,check=True,timeout=240):
  r=subprocess.run(['docker',*map(str,args)],input=source,capture_output=True,text=True,timeout=timeout)
  if check and r.returncode:raise RuntimeError('Synthetic restore command failed; private output suppressed')
  return r
 def sql(side,query):return logical.sql(prefix+'-'+side,query)
 def migrate(side,baseline=False):
  args=['run','--rm','--platform','linux/amd64','--network',prefix+'-'+side+'-net','--memory','512m','--cpus','.5','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--tmpfs','/tmp:rw,size=32m','-v',f'{cred}:/credentials:ro','-v',f'{cred}:/tls:ro']
  if baseline:args+=['--entrypoint','/bin/bash']
  args+=[image]
  if baseline:args+=['-ec',"test $(grep -c '^flyway.target=35$' /schema/migrate.sh) -eq 1; sed 's/^flyway.target=35$/flyway.target=32/;s/through V35/through V32 fixture/' /schema/migrate.sh > /tmp/v32.sh; /bin/bash /tmp/v32.sh"]
  r=docker(*args,check=False)
  if r.returncode:
   (output/('flyway-'+side+'.log')).write_text(r.stdout+r.stderr);raise RuntimeError('Synthetic Flyway failed; see fixture diagnostic')
 try:
  cred=directory/'credentials';cred.mkdir();p=bootstrap.create_payload(cred);cred.chmod(0o755)
  for key,name in [('rootPassword','root-password'),('appPassword','app-password')]: (cred/name).write_text(p[key])
  for f in cred.iterdir():f.chmod(0o444) # Disposable test-only credentials for UID999.
  for side,mysql in [('source',logical.SOURCE_IMAGE),('target',logical.TARGET_IMAGE)]:
   name=prefix+'-'+side;net=name+'-net';networks.append(net)
   docker('network','create','--internal','--label','yeodam.issue=591',net)
   data=directory/side;data.mkdir();data.chmod(0o777);containers.append(name)
   docker('run','-d','--platform','linux/amd64','--name',name,'--label','yeodam.issue=591','--network',net,'--network-alias','mysql-primary.mysql.svc.cluster.local','--memory','768m','--cpus','1','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--user','999:999','--tmpfs','/tmp:rw,size=128m','--tmpfs','/var/run/mysqld:rw,size=8m','-v',f'{cred}:/credentials:ro','-v',f'{cred}:/tls:ro','-v',f'{data}:/var/lib/mysql','-e','MYSQL_ROOT_PASSWORD_FILE=/credentials/root-password','-e','MYSQL_DATABASE=yeodam','-e','MYSQL_USER=yeodam','-e','MYSQL_PASSWORD_FILE=/credentials/app-password',mysql,'--socket=/tmp/mysql.sock','--pid-file=/tmp/mysql.pid','--innodb-buffer-pool-size=64M','--innodb-redo-log-capacity=100M','--max-connections=20','--require-secure-transport=ON','--ssl-ca=/tls/ca.crt','--ssl-cert=/tls/tls.crt','--ssl-key=/tls/tls.key','--server-id=1','--gtid-mode=ON','--enforce-gtid-consistency=ON','--log-bin=mysql-bin')
   for _ in range(120):
    r=docker('exec','-i',name,'/bin/bash','-ec',logical.ROOT_SHELL,source='SELECT VERSION();',check=False)
    if not r.returncode and r.stdout.strip()==('9.7.2' if side=='source' else '8.4.13'):
     # Ignore the official entrypoint's temporary initialization server.
     if docker('exec',name,'/bin/bash','-ec',"tr '\\0' ' ' </proc/1/cmdline").stdout.startswith('mysqld '):break
    time.sleep(2)
   else:raise RuntimeError('Fixture database initialization failed')
  migrate('source',True)
  sql('source',"""SET NAMES utf8mb4;
INSERT INTO yeodam.users(user_id,email,nickname) VALUES(91001,'restore@example.invalid','여행 복원');
INSERT INTO yeodam.trips(trip_id,user_id,trip_name,start_date,end_date) VALUES(92001,91001,'제주 ''사진'' 여행','2026-01-01','2026-01-02');
INSERT INTO yeodam.story_generation_jobs(generation_id,execution_id,trip_id,user_id,mood,status,progress,created_at,updated_at) VALUES(93001,'logical-restore-fixture',92001,91001,'CALM','QUEUED',0,'2026-01-01 00:00:00','2026-01-01 00:00:00');
SET GLOBAL read_only=ON; SET GLOBAL super_read_only=ON;""")
  package=directory/'package';manifest=logical.backup(prefix+'-source',package);assert len(manifest['tables'])==32
  bad=directory/'bad';shutil.copytree(package,bad)
  with (bad/'yeodam.sql.gz').open('ab') as f:f.write(b'tamper')
  try:logical.restore(prefix+'-target',bad)
  except ValueError:pass
  else:raise AssertionError('Tampered backup accepted')
  assert not logical.catalog(prefix+'-target')
  restored=logical.restore(prefix+'-target',package)
  try:logical.restore(prefix+'-target',package)
  except ValueError:pass
  else:raise AssertionError('Nonempty target accepted')
  migrate('target')
  assert sql('target',"SELECT CONCAT(COUNT(*),':',SUM(success),':',MAX(CAST(version AS UNSIGNED))) FROM yeodam.flyway_schema_history;")=='35:35:35'
  assert logical.capture(prefix+'-target',manifest['tables'])==manifest['tables']
  assert sql('target','SELECT prepared_content_metadata IS NULL FROM yeodam.story_generation_jobs WHERE generation_id=93001;')=='1'
  assert logical.foreign_key_violations(prefix+'-target')==0
  new=set(logical.catalog(prefix+'-target'))-set(manifest['tables']);assert len(new)==2
  for table in new:assert sql('target','SELECT COUNT(*) FROM yeodam.'+logical.identifier(table))=='0'
  migrate('target');assert logical.capture(prefix+'-target',manifest['tables'])==manifest['tables']
  assert logical.capture(prefix+'-source')==manifest['tables']
  assert sql('source','SELECT @@global.read_only AND @@global.super_read_only;')=='1'
  (output/'result.json').write_text(json.dumps({**restored,'sourceEngine':'9.7.2','sourceSchema':32,'targetEngine':'8.4.13','targetSchema':35,'tamperRejectedBeforeImport':True,'nonemptyTargetRejected':True,'foreignKeyViolations':0,'newTablesEmpty':True,'v35RerunNoop':True,'sourceUnchangedAndFrozen':True,'syntheticOnly':True},indent=2))
  print('PASS: synthetic V32 logical restore to V35, row/history hashes, FK, tamper and overwrite refusal',flush=True)
 finally:
  for name in reversed(containers):
   r=docker('logs',name,check=False);(output/(name+'.log')).write_text(r.stdout+r.stderr);docker('rm','-f',name,check=False)
  for net in reversed(networks):docker('network','rm',net,check=False)
  for side in ['source','target']:
   data=directory/side
   if data.exists():docker('run','--rm','--platform','linux/amd64','--network','none','--read-only','--cap-drop','ALL','--user','999:999','-v',f'{data}:/cleanup','--entrypoint','/bin/bash',logical.TARGET_IMAGE,'-ec','rm -rf /cleanup/* /cleanup/.[!.]* /cleanup/..?*',check=False)
  shutil.rmtree(directory)
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--image',required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();smoke(a.image,a.output)
