#!/usr/bin/env python3
"""Read only the single Argo Application over an authenticated EKS TLS tunnel."""
import argparse,base64,http.client,json,os,re,socket,ssl,subprocess,threading,time
from pathlib import Path
from v3_delivery_contract import UNITS,APP_NAMES


def require(ok,message):
 if not ok:raise ValueError(message)


def status(application,commit):
 s=application.get('spec',{});source=s.get('source',{})
 require(application.get('metadata',{}).get('namespace')=='argocd'and application['metadata']['name']=='yeodam-v3-apps'
         and s.get('project')=='yeodam-v3-apps'and source.get('repoURL')=='https://github.com/100-hours-a-week/KTB4-2nd-Cloud.git'
         and source.get('targetRevision')=='deploy/v3-approved'and source.get('path')=='manifests','Unexpected Application source or destination')
 state=application.get('status',{});sync=state.get('sync',{});health=state.get('health',{}).get('status','Unknown')
 deployments=[x for x in state.get('resources',[])if x.get('kind')=='Deployment']
 desired={(ns,name)for name,ns in APP_NAMES.items()}
 healthy=len(deployments)==9 and {(x.get('namespace'),x['name'])for x in deployments}==desired and all(x.get('status')=='Synced'and x.get('health',{}).get('status')=='Healthy'for x in deployments)
 ready=sync.get('revision')==commit and sync.get('status')=='Synced'and health=='Healthy'and healthy
 return {'deployed':ready,'sync':sync.get('status','Unknown'),'health':health,'healthyDeployments':sum(x.get('health',{}).get('status')=='Healthy'for x in deployments),'expectedCommitObserved':sync.get('revision')==commit}


def aws(*args):
 r=subprocess.run(['aws',*args,'--region','ap-northeast-2','--output','json'],capture_output=True,text=True,check=True,timeout=30)
 return json.loads(r.stdout)


class TunnelConnection(http.client.HTTPSConnection):
 def connect(self):
  raw=socket.create_connection(('127.0.0.1',18573),timeout=self.timeout)
  self.sock=self._context.wrap_socket(raw,server_hostname=self.host)


def observe(commit,instance,timeout):
 require(re.fullmatch('[a-f0-9]{40}',commit)and re.fullmatch('i-[a-f0-9]{17}',instance),'Exact commit and system instance required')
 require(30<=timeout<=900,'Bounded observer timeout')
 identity=aws('sts','get-caller-identity')
 require(identity['Account']=='483175530259'and re.fullmatch(r'arn:aws:sts::483175530259:assumed-role/yeodam-v3-foundation-delivery-status/v3-cd-[0-9]+',identity['Arn']),'Use only the environment-approved read-only observer role')
 c=aws('eks','describe-cluster','--name','yeodam-v3-foundation')['cluster']
 require(c['status']=='ACTIVE'and c['resourcesVpcConfig']['vpcId']=='vpc-0ecc3132f3b2170e9','Wrong V3 cluster')
 from urllib.parse import urlparse
 host=urlparse(c['endpoint']).hostname
 context=ssl.create_default_context(cadata=base64.b64decode(c['certificateAuthority']['data']).decode())
 sessions=[]
 process=subprocess.Popen(['aws','ssm','start-session','--region','ap-northeast-2','--target',instance,'--document-name','Yeodam-V3-Delivery-Status'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
 def consume():
  for line in process.stdout:
   match=re.search(r'Starting session with SessionId: ([A-Za-z0-9_:-]+)',line)
   if match:sessions.append(match[1])
 thread=threading.Thread(target=consume,daemon=True);thread.start();report={'deployed':False,'sync':'Unknown','health':'Unknown'}
 try:
  start=time.monotonic()
  while True:
   require(process.poll()is None,'Management tunnel exited')
   try:
    with socket.create_connection(('127.0.0.1',18573),timeout=.2):break
   except OSError:
    require(time.monotonic()-start<45,'Management tunnel did not become ready');time.sleep(.2)
  token='';issued=0
  while time.monotonic()-start<timeout:
   if not token or time.monotonic()-issued>480:
    token=aws('eks','get-token','--cluster-name','yeodam-v3-foundation')['status']['token'];issued=time.monotonic()
   connection=TunnelConnection(host,timeout=10,context=context)
   try:
    connection.request('GET','/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/yeodam-v3-apps',headers={'Authorization':'Bearer '+token})
    response=connection.getresponse();require(response.status==200,'Read-only Application request denied or unavailable');app=json.loads(response.read())
   finally:connection.close()
   report=status(app,commit)
   if report['deployed']:return report|{'approvedCommit':commit,'status':'deployed'}
   time.sleep(10)
  return report|{'approvedCommit':commit,'status':'timeout'}
 finally:
  process.terminate()
  try:process.wait(timeout=5)
  except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
  thread.join(timeout=2)
  require(len(sessions)==1,'Could not identify this management session for cleanup')
  aws('ssm','terminate-session','--session-id',sessions[0])


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--commit',required=True);p.add_argument('--instance',required=True);p.add_argument('--timeout',type=int,default=900);p.add_argument('--report',required=True,type=Path);a=p.parse_args()
 try:result=observe(a.commit,a.instance,a.timeout)
 except Exception as e:result={'status':'failed','deployed':False,'reason':str(e)if isinstance(e,ValueError)else type(e).__name__}
 a.report.parent.mkdir(parents=True,exist_ok=True);a.report.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
 raise SystemExit(0 if result['deployed']else 1)


if __name__=='__main__':main()
