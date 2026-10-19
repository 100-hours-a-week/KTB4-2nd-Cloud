#!/usr/bin/env python3
"""Change only an idle process runner. Reserve its existing Redis slot before recreation."""
import argparse, fcntl, json, os, re, secrets, socket, subprocess, time, urllib.parse
from pathlib import Path
SETTINGS={
 'staging':{'container':'yeodam-ai-process-runner','root':'/opt/yeodam-ai','env':'/etc/yeodam/ai/runner.env','redis':'ai-redis.staging.yeodam.internal','lock':'/run/yeodam-ai-runner-deploy.lock'},
 'production':{'container':'yeodam-ai-process-runner-production','root':'/opt/yeodam-production-runner','env':'/etc/yeodam/production-ai/runner.env','redis':'ai-redis.production.yeodam.internal','lock':'/run/yeodam-production-runner.lock'}}
class Redis:
    def __init__(self,url,host):
        u=urllib.parse.urlparse(url)
        if (u.scheme,u.hostname,u.port or 6379,u.path) != ('redis',host,6379,'/0'):raise RuntimeError('Unexpected Redis destination')
        self.sock=socket.create_connection((host,6379),timeout=4);self.file=self.sock.makefile('rb')
        if u.password:
            auth=['AUTH']
            if u.username:auth.append(urllib.parse.unquote(u.username))
            self.command(*auth,urllib.parse.unquote(u.password))
    def read(self):
        line=self.file.readline(1048576)
        if not line.endswith(b'\r\n'):raise RuntimeError('Invalid Redis response')
        kind,payload=line[:1],line[1:-2]
        if kind==b'-':raise RuntimeError('Redis command rejected')
        if kind==b':':return int(payload)
        if kind==b'+':return payload.decode()
        if kind==b'$':
            n=int(payload)
            if n==-1:return None
            if not 0<=n<=1048576:raise RuntimeError('Redis reply too large')
            body=self.file.read(n+2)
            if len(body)!=n+2:raise RuntimeError('Incomplete Redis response')
            return body[:-2].decode()
        raise RuntimeError('Unexpected Redis response type')
    def command(self,*args):
        parts=[str(x).encode() for x in args];self.sock.sendall(b'*'+str(len(parts)).encode()+b'\r\n'+b''.join(b'$'+str(len(x)).encode()+b'\r\n'+x+b'\r\n' for x in parts));return self.read()
    def close(self):self.file.close();self.sock.close()
RESERVE="if redis.call('ZCARD',KEYS[2])>0 then return -1 end; if redis.call('SET',KEYS[1],ARGV[1],'NX','PX',300000) then return 1 else return 0 end"
RELEASE="if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) else return 0 end"
def docker(*args,cwd=None):
    r=subprocess.run(['docker',*args],cwd=cwd,capture_output=True,text=True,timeout=35)
    if r.returncode:raise RuntimeError('Docker operation failed; inspect locally without printing environment')
    return r.stdout

def inspect(cfg):
    row=json.loads(docker('inspect',cfg['container']))[0]
    if row['Config']['Cmd']!=['python','-m','app.jobs.runner'] or not row['State']['Running']:raise RuntimeError('Expected running process runner')
    values=dict(x.split('=',1) for x in row['Config']['Env'] if '=' in x)
    level=values.get('LOG_LEVEL','INFO')
    contract_code="import inspect,json; from app.jobs.redis_store import RedisJobStore; from app.jobs.config import JobsSettings; c=JobsSettings(); s=inspect.getsource(RedisJobStore.claim); print(json.dumps({'prefix':c.redis_prefix,'slots':RedisJobStore(None,c).slot_keys('process'),'safe':all(x in s for x in ['pipe.watch(queue, *slots)','occupants = await pipe.mget(slots)','if raw and JobRecord.model_validate_json(raw).status in TERMINAL_STATUSES:','if sum(owner is not None for owner in occupants) >= limit:'])}))"
    contract=json.loads(docker('exec',cfg['container'],'python','-c',contract_code))
    expected='yeodam:'+values.get('JOBS_ENV','')+':ai:'
    if not contract['safe'] or contract['prefix']!=expected or contract['slots']!=[expected+'slot:process']:raise RuntimeError('Runner slot contract changed; review before using this tool')
    configured=json.loads(docker('compose','config','--format','json',cwd=cfg['root']))['services']['process-runner']
    if configured['image']!=row['Config']['Image']:raise RuntimeError('Compose image differs; reconcile deployment first')
    override=Path(cfg['root'])/'compose.override.yml'
    if override.exists() and 'LOG_LEVEL' in json.loads(override.read_text()).get('services',{}).get('process-runner',{}).get('environment',{}):raise RuntimeError('Remove unmanaged Compose LOG_LEVEL override before using this tool')
    row['_configured_level']=configured.get('environment',{}).get('LOG_LEVEL','INFO')
    return row,values,level

def atomic(path,content):
    tmp=path.with_suffix('.next');tmp.write_text(content);tmp.chmod(0o600);tmp.replace(path)

def updated_env(content,level):
    lines=[x for x in content.splitlines() if not re.match(r'^\s*(?:export\s+)?LOG_LEVEL=',x)]
    return '\n'.join(lines)+'\nLOG_LEVEL='+level+'\n'

def change(env,level,inspect_only=False):
    cfg=SETTINGS[env];os.umask(0o077)
    with open(cfg['lock'],'w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        before,values,current=inspect(cfg)
        if values.get('JOBS_ENV')!=env:raise RuntimeError('Runner environment mismatch')
        client=Redis(values['REDIS_URL'],cfg['redis']);prefix='yeodam:'+env+':ai:';slot=prefix+'slot:process';queue=prefix+'queue:process';marker='cloud-log-level:'+secrets.token_hex(16)
        try:
            if inspect_only:
                print(json.dumps({'env':env,'level':current,'configuredLevel':before.get('_configured_level',current),'queue':client.command('ZCARD',queue),'slotOccupied':client.command('EXISTS',slot)==1}));return
            if current==level:
                reconciled=before.get('_configured_level',current)!=current
                if reconciled:
                    path=Path(cfg['env']);atomic(path,updated_env(path.read_text(),level))
                print(json.dumps({'env':env,'level':current,'changed':False,'configReconciled':reconciled}));return
            acquired=client.command('EVAL',RESERVE,2,slot,queue,marker)
            if acquired!=1:raise RuntimeError('Runner busy or queued work exists; wait for completion and retry')
            journal=Path('/var/lib/yeodam/ai-runner-level')/(env+'.json');journal.parent.mkdir(parents=True,exist_ok=True)
            path=Path(cfg['env']);old=path.read_text();changed=False
            try:
                # WATCH in RedisJobStore.claim observes this slot; new requests remain queued.
                if client.command('GET',slot)!=marker:raise RuntimeError('Idle reservation lost')
                atomic(journal,json.dumps({'phase':'reserved','previousLevel':current,'requestedLevel':level,'image':before['Config']['Image'],'at':time.time()}))
                atomic(path,updated_env(old,level));changed=True
                docker('compose','up','-d','--no-deps','--force-recreate','process-runner',cwd=cfg['root'])
                after,_,actual=inspect(cfg)
                if after['Config']['Image']!=before['Config']['Image'] or actual!=level:raise RuntimeError('Runner verification failed')
                probe="import os,redis,httpx; assert redis.Redis.from_url(os.environ['REDIS_URL'],socket_connect_timeout=3,socket_timeout=3).ping(); httpx.get(os.environ['EMBEDDING_BASE_URL'].removesuffix('/v1')+'/health',timeout=8).raise_for_status(); httpx.get(os.environ['QDRANT_URL']+'/readyz',timeout=8).raise_for_status()"
                docker('exec',cfg['container'],'python','-c',probe)
                atomic(journal,json.dumps({'phase':'complete','previousLevel':current,'level':actual,'image':after['Config']['Image'],'at':time.time()}))
                print(json.dumps({'env':env,'level':actual,'previousLevel':current,'changed':True}))
            except Exception:
                if changed:
                    atomic(path,old)
                    try:docker('compose','up','-d','--no-deps','--force-recreate','process-runner',cwd=cfg['root'])
                    except Exception:raise RuntimeError('Rollback could not restore runner; inspect journal and environment locally') from None
                raise
            finally:client.command('EVAL',RELEASE,1,slot,marker)
        finally:client.close()
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--environment',choices=SETTINGS,required=True);p.add_argument('--level',choices=['DEBUG','INFO'],default='INFO');p.add_argument('--inspect',action='store_true');a=p.parse_args()
    if os.geteuid()!=0:raise SystemExit('Run via SSM as root')
    try:change(a.environment,a.level,a.inspect)
    except Exception as e:raise SystemExit(type(e).__name__+': '+str(e)) from None
