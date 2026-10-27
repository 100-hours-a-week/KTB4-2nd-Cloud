#!/usr/bin/env python3
"""Real dual-stack kernel test in disposable namespaces, never the host firewall."""
import importlib.util,json,os,subprocess,tempfile,time,uuid
from pathlib import Path

def run(args,**kw):return subprocess.run(args,check=True,capture_output=True,text=True,timeout=30,**kw).stdout
def main():
 if os.geteuid()!=0:raise SystemExit('Root and ip/nft required; isolated namespaces only')
 root=Path(__file__).resolve().parents[2]
 module=root/'monitoring/v1-staging/nginx_tcp_guard.py'
 if not module.exists():module=Path('/opt/yeodam/ops/nginx-tcp436/nginx_tcp_guard.py')
 spec=importlib.util.spec_from_file_location('guard',module);guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
 suffix=uuid.uuid4().hex[:6];client='n436c'+suffix;server='n436s'+suffix
 a,b='v436c'+suffix,'v436s'+suffix
 process=None;created=[]
 try:
  for name in [client,server]:run(['ip','netns','add',name]);created.append(name)
  run(['ip','link','add',a,'type','veth','peer','name',b])
  for dev,namespace,v4,v6 in [(a,client,'198.18.43.1/30','fd00:436::1/64'),(b,server,'198.18.43.2/30','fd00:436::2/64')]:
   run(['ip','link','set',dev,'netns',namespace]);run(['ip','-n',namespace,'link','set','lo','up'])
   run(['ip','-n',namespace,'addr','add',v4,'dev',dev]);run(['ip','-n',namespace,'-6','addr','add',v6,'dev',dev,'nodad'])
   run(['ip','-n',namespace,'link','set',dev,'up'])
  code="""import selectors,socket
sel=selectors.DefaultSelector()
for family,addr in [(socket.AF_INET,'198.18.43.2'),(socket.AF_INET6,'fd00:436::2')]:
 s=socket.socket(family);s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind((addr,443));s.listen(128);s.setblocking(False);sel.register(s,selectors.EVENT_READ,True)
print('ready',flush=True)
while True:
 for key,event in sel.select(1):
  s=key.fileobj
  if key.data:
   c,_=s.accept();c.setblocking(False);sel.register(c,selectors.EVENT_READ,False)
  elif not s.recv(1024):sel.unregister(s);s.close()
"""
  process=subprocess.Popen(['ip','netns','exec',server,'python3','-u','-c',code],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  if process.stdout.readline().strip()!='ready':raise RuntimeError('Fixture not ready')
  policy=guard.nft_policy(16,0,10,True)
  run(['ip','netns','exec',server,'nft','-f','-'],input=policy)
  code="""import socket,json,time
out=[]
for family,addr in [(socket.AF_INET,'198.18.43.2'),(socket.AF_INET6,'fd00:436::2')]:
 peers=[];ok=0;failed=0
 for i in range(17):
  s=socket.socket(family);s.settimeout(.25)
  try:s.connect((addr,443));peers.append(s);ok+=1
  except OSError:s.close();failed+=1
 for s in peers:s.close()
 time.sleep(2)
 s=socket.socket(family);s.settimeout(.5)
 try:s.connect((addr,443));recovered=True
 except OSError:recovered=False
 s.close()
 out.append({'family':family,'opened':ok,'blocked':failed,'recoveredAfterTwoSeconds':recovered})
print(json.dumps(out))
"""
  result={'concurrency':json.loads(run(['ip','netns','exec',client,'python3','-c',code]))}
  run(['ip','netns','exec',server,'nft','delete','table','inet',guard.TABLE])
  run(['ip','netns','exec',server,'nft','-f','-'],input=guard.nft_policy(0,10,10,True))
  code="""import socket,json
out=[]
for family,addr in [(socket.AF_INET,'198.18.43.2'),(socket.AF_INET6,'fd00:436::2')]:
 ok=0;failed=0
 for i in range(40):
  s=socket.socket(family);s.settimeout(.04)
  try:s.connect((addr,443));ok+=1
  except OSError:failed+=1
  finally:s.close()
 out.append({'family':family,'opened':ok,'blocked':failed})
print(json.dumps(out))
"""
  result['rate']=json.loads(run(['ip','netns','exec',client,'python3','-c',code]))
  result['scope']='Isolated Linux namespaces, IPv4/IPv6 kernel behavior; not public IPv6 staging availability'
  result['passed']=all(x['opened']==16 and x['blocked']==1 and x['recoveredAfterTwoSeconds'] for x in result['concurrency']) and all(x['opened']>0 and x['blocked']>0 for x in result['rate'])
  print(json.dumps(result))
  if not result['passed']:raise SystemExit(1)
 finally:
  if process:
   process.terminate()
   try:process.wait(timeout=3)
   except subprocess.TimeoutExpired:process.kill();process.wait()
  for name in reversed(created):subprocess.run(['ip','netns','delete',name],capture_output=True)

if __name__=='__main__':main()
