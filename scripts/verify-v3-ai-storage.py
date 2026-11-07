#!/usr/bin/env python3
"""Small isolated persistence test. Run write -> controlled Pod recreation -> verify -> cleanup.
Credentials directory must contain ca.crt and app-password (Redis) or api-key (Qdrant).
Port-forward verifies TLS/auth/data, not NetworkPolicy. No production target accepted.
"""
import argparse,http.client,json,re,socket,ssl
from pathlib import Path
class RedisError(Exception):pass
class Client:
    def __init__(self,store,directory,connect_host,port):
        self.store=store;self.directory=directory;self.host=f'{store}.{store}.svc.cluster.local';self.port=port;self.connect_host=connect_host or self.host
        if connect_host and connect_host not in ['127.0.0.1','localhost']:raise ValueError('Override is only for local V3 port-forward')
        self.context=ssl.create_default_context(cafile=str(directory/'ca.crt'))
    def socket(self):return self.context.wrap_socket(socket.create_connection((self.connect_host,self.port),timeout=10),server_hostname=self.host)
    def http(self,method,path,value=None,key=True):
        conn=http.client.HTTPSConnection(self.host,self.port,timeout=10,context=self.context);conn.sock=self.socket()
        headers={'Content-Type':'application/json'}
        if key:headers['api-key']=(self.directory/'api-key').read_text().strip()
        try:
            conn.request(method,path,body=json.dumps(value) if value is not None else None,headers=headers)
            r=conn.getresponse();raw=r.read()
            try:value=json.loads(raw) if raw else {}
            except ValueError:value={}
            return r.status,value
        finally:conn.close()
    def redis_open(self,auth=True):
        self.sock=self.socket();self.file=self.sock.makefile('rb')
        if auth:self.command('AUTH','app',(self.directory/'app-password').read_text().strip())
    def read(self):
        line=self.file.readline();kind=line[:1];value=line[1:-2]
        if kind==b'-':raise RedisError('Redis rejected request')
        if kind==b'+':return value.decode()
        if kind==b':':return int(value)
        if kind==b'$':
            size=int(value)
            if size==-1:return None
            raw=self.file.read(size);assert self.file.read(2)==b'\r\n';return raw.decode()
        if kind==b'*':return [self.read() for _ in range(int(value))]
        raise RedisError('Unexpected Redis response')
    def command(self,*args):
        values=[str(x).encode() for x in args];self.sock.sendall(b'*'+str(len(values)).encode()+b'\r\n'+b''.join(b'$'+str(len(x)).encode()+b'\r\n'+x+b'\r\n' for x in values));return self.read()
    def close(self):self.file.close();self.sock.close()
def exercise(client,phase,run_id):
    if not re.fullmatch(r'v3-smoke-554-[a-f0-9]{8}',run_id):raise ValueError('Only isolated #554 test names accepted')
    if client.store=='ai-redis':
        client.redis_open(False)
        try:
            try:client.command('GET',run_id);raise AssertionError('Unauthenticated read allowed')
            except RedisError:pass
            try:client.command('AUTH','default','not-a-credential');raise AssertionError('Disabled default user accepted')
            except RedisError:pass
        finally:client.close()
        client.redis_open()
        try:
            try:client.command('CONFIG','GET','maxmemory');raise AssertionError('App admin command allowed')
            except RedisError:pass
            if phase=='write':
                client.command('MULTI');client.command('HSET',run_id+':state','status','queued');client.command('ZADD',run_id+':queue',1,'item1');client.command('EXPIRE',run_id+':state',3600);client.command('EXPIRE',run_id+':queue',3600);client.command('EXEC')
            if phase in ['write','verify']:
                assert client.command('HGET',run_id+':state','status')=='queued';assert client.command('ZRANGE',run_id+':queue',0,-1)==['item1']
            else:client.command('DEL',run_id+':state',run_id+':queue');assert client.command('EXISTS',run_id+':state',run_id+':queue')==0
        finally:client.close()
    else:
        status,_=client.http('GET','/collections',key=False);assert status in [401,403],'Unauthenticated Qdrant request allowed'
        path='/collections/'+run_id
        if phase=='write':
            status,_=client.http('PUT',path,{'vectors':{'size':3,'distance':'Cosine','on_disk':True}});assert status==200
            status,_=client.http('PUT',path+'/points?wait=true',{'points':[{'id':1,'vector':[1.0,0.0,0.0],'payload':{'test':run_id}}]});assert status==200
        if phase in ['write','verify']:
            status,r=client.http('GET',path+'/points/1?with_vector=true');assert status==200 and r['result']['payload']['test']==run_id
            status,r=client.http('POST',path+'/points/query',{'query':[1.0,0.0,0.0],'limit':1});assert status==200 and r['result']['points'][0]['id']==1
        else:
            status,_=client.http('DELETE',path);assert status==200
            status,_=client.http('GET',path);assert status==404
    return {'store':client.store,'phase':phase,'tlsAndHostnameVerified':True,'unauthenticatedRejected':True,'testDataOnly':True}
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--store',required=True,choices=['ai-redis','qdrant']);p.add_argument('--credentials-directory',required=True,type=Path);p.add_argument('--connect-host');p.add_argument('--port',type=int);p.add_argument('--phase',required=True,choices=['write','verify','cleanup']);p.add_argument('--run-id',required=True);a=p.parse_args()
    try:print(json.dumps(exercise(Client(a.store,a.credentials_directory,a.connect_host,a.port or (6379 if a.store=='ai-redis' else 6333)),a.phase,a.run_id)))
    except Exception as e:raise SystemExit('Storage check failed ('+type(e).__name__+'); no response or credentials printed')
