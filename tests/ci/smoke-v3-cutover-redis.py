#!/usr/bin/env python3
"""Actual pinned Redis7 CLI, synthetic TLS/ACL only, no AWS or host ports."""
import importlib.util,json,re,shutil,subprocess,tempfile,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
s=importlib.util.spec_from_file_location('fixture',ROOT/'scripts/prepare-v3-ai-storage-secrets.py');fixture=importlib.util.module_from_spec(s);s.loader.exec_module(fixture)
image=re.search(r'^  redis: (.+)$',(ROOT/'deploy/v3/ai-storage/values.yaml').read_text(),re.M)[1]
name='v3-cutover-redis-'+uuid.uuid4().hex[:10];script=(ROOT/'scripts/cutover/redis.sh').read_bytes()
def command(*args,source=None,check=True):
 r=subprocess.run(list(map(str,args)),input=source,capture_output=True,timeout=90)
 if check and r.returncode:raise RuntimeError('Synthetic Redis cutover contract failed; no secret output')
 return r
with tempfile.TemporaryDirectory(prefix=name) as tmp:
 directory=Path(tmp);directory.chmod(0o755);fixture.prepare(directory/'credentials');cred=directory/'credentials/ai-redis'
 payload=json.loads((cred/'source.json').read_text())
 for key in ['app','admin','health']:(cred/(key+'-password')).write_text(payload[key+'Password'])
 # Disposable fixture permissions only; no existing credentials are read.
 (directory/'credentials').chmod(0o755);cred.chmod(0o755)
 for p in cred.iterdir():p.chmod(0o444)
 def cli(*args,check=True):
  shell='export REDISCLI_AUTH="$(cat /credentials/admin-password)"; exec redis-cli --no-auth-warning --tls --cacert /credentials/ca.crt --sni 127.0.0.1 -h 127.0.0.1 --user admin --raw -e "$@"'
  return command('docker','exec',name,'/bin/sh','-ec',shell,'redis-test',*args,check=check).stdout.strip()
 def run(action,check=True):return command('docker','exec','-i',name,'/bin/bash','-s','--',action,source=script,check=check)
 try:
  command('docker','run','-d','--name',name,'--network','none','--memory','128m','--cpus','.25','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--user','999:999','--tmpfs','/tmp:rw,size=8m','--tmpfs','/data:rw,size=16m,uid=999,gid=999','-v',str(cred)+':/credentials:ro','-v',str(ROOT/'deploy/v3/ai-storage/files/redis')+':/runtime:ro','--entrypoint','/bin/sh',image,'/runtime/start.sh')
  for _ in range(40):
   try:
    if cli('PING',check=False)==b'PONG':break
   except RuntimeError:pass
   time.sleep(.25)
  else:raise RuntimeError('Synthetic Redis did not start')
  assert run('queue-count').stdout.strip()==b'0'
  cli('ZADD','fixture queue','1','a','2','b');cli('ZADD','second','3','c')
  cli('XADD','events','*','field','value');cli('XADD','events','*','field','value')
  cli('SET','unrelated','preserve')
  before=cli('DBSIZE');assert run('queue-count').stdout.strip()==b'5'
  assert cli('DBSIZE')==before and cli('GET','unrelated')==b'preserve'
  old=script.replace(b'keys=$(cli --scan);',b'keys=$(cli --scan --type zset);')
  assert command('docker','exec','-i',name,'/bin/bash','-s','--','queue-count',source=old,check=False).returncode!=0
  assert run('backup').stdout.startswith(b'REDIS') and cli('DBSIZE')==before
  cli('-n','1','SET','protected','remain')
  assert run('clear',False).returncode!=0 and cli('DBSIZE')==before
  cli('-n','1','FLUSHDB');assert run('clear').stdout.strip()==b'CLEARED'
  assert run('queue-count').stdout.strip()==b'0'
  print('PASS: Redis7 unsupported option reproduced; TLS aggregate is read-only; RDB, protected DB and clear boundaries verified')
 finally:command('docker','rm','-f',name,check=False)
