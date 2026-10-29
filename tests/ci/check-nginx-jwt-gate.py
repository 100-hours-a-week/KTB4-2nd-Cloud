#!/usr/bin/env python3
"""Finite functional fixtures inside one network-isolated container, never staging load."""
import base64
import hashlib
import hmac
from http.client import HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
import subprocess
import threading
import time

ROOT = Path('/work')
config = json.loads((ROOT/'config.json').read_text())
requests = []
class Backend(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args): pass
    def respond(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        # Synthetic stand-in for BE SID checking: edge admission must not grant this session access.
        sid = ''
        cookie = self.headers.get('Cookie', '')
        if 'accessToken=' in cookie:
            try:
                token = cookie.split('accessToken=', 1)[1].split(';', 1)[0]
                sid = json.loads(base64.urlsafe_b64decode(token.split('.')[1]+'=='))['sid']
            except Exception: pass
        status = 401 if sid == 'revoked-session' else 200
        requests.append({'path':self.path,'method':self.command,'status':status,
                         'requestId':self.headers.get('X-Request-ID'), 'bodyBytes':len(body), 'port':self.server.server_port})
        payload = json.dumps({'message':'MOCK_BACKEND','data':{'path':self.path,'bodyBytes':len(body)}}).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(payload)))
        self.end_headers()
        if self.command != 'HEAD': self.wfile.write(payload)
    do_GET=respond
    do_HEAD=respond
    do_POST=respond
    do_OPTIONS=respond

servers=[]
for port in [8080,3000]:
    server=ThreadingHTTPServer(('127.0.0.1',port),Backend)
    threading.Thread(target=server.serve_forever,daemon=True).start();servers.append(server)
context=ssl._create_unverified_context()
client=None
checks=[]
def request(path='/api/trips', token=None, method='GET', body=None, extra=None):
    global client
    if client is None: client=HTTPSConnection('127.0.0.1',443,context=context,timeout=4)
    headers={'Host':'v1-staging.yeodam-2gether.com'}
    if token is not None: headers['Cookie']='accessToken='+token
    headers.update(extra or {})
    client.request(method,path,body=body,headers=headers)
    response=client.getresponse();data=response.read()
    return response.status,dict((k.lower(),v) for k,v in response.getheaders()),data

def token(user='101',sid='live-session',**patch):
    now=int(time.time())
    claims={'iss':config['issuer'],'aud':[config['audience']],'sub':user,'sid':sid,'iat':now,'exp':now+300}
    claims.update(patch)
    header={'alg':'HS256','typ':'JWT'}
    def encode(data):return base64.urlsafe_b64encode(json.dumps(data,separators=(',',':')).encode()).decode().rstrip('=')
    payload=encode(header)+'.'+encode(claims)
    return payload+'.'+base64.urlsafe_b64encode(hmac.new(config['secret'].encode(),payload.encode(),hashlib.sha256).digest()).decode().rstrip('=')

def check(name, condition):
    assert condition,name
    checks.append(name)

nginx=subprocess.Popen(['nginx','-c','/work/nginx.conf','-g','daemon off;'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
try:
    for _ in range(50):
        try:
            if request('/api/actuator/health')[0]==200:break
        except OSError: time.sleep(.05);client=None
    else: raise AssertionError('Nginx not ready')
    before=len(requests)
    forged=token();parts=forged.split('.');parts[2]=('A' if parts[2][0]!='A' else 'B')+parts[2][1:]
    missing=request();bad=request(token='.'.join(parts));expired=request(token=token(exp=int(time.time())-61))
    check('missing/tampered/expired return 401 without upstream',all(x[0]==401 for x in [missing,bad,expired]) and len(requests)==before)
    valid=request('/api/trips?cursor=a%2Fb',token=token())
    check('original URI and request ID preserved',valid[0]==200 and requests[-1]['path']=='/api/trips?cursor=a%2Fb' and bool(requests[-1]['requestId']))
    check('HEAD forwarded with no body',request(token=token(user='102'),method='HEAD')[0]==200)
    before=len(requests)
    check('read request body rejected before JS buffering',request(token=token(user='103'),body='nonempty')[0]==413 and len(requests)==before)
    # Fresh key, then exceed its small burst. This is a finite functional assertion, not a capacity test.
    a=token(user='200');a2=token(user='200',sid='other-session')
    responses=[request(token=a)[0] for _ in range(42)]
    check('valid-user overage blocked at Nginx',429 in responses)
    check('same user different SID shares edge quota',request(token=a2)[0]==429)
    check('same IP different user has independent quota',request(token=token(user='201'))[0]==200)
    # Invalid bucket must not consume signature-valid bucket, even with forged caller identity.
    before=len(requests)
    outcomes=[request(extra={'X-User-ID':'201','X-Verified-User':'201'})[0] for _ in range(82)]
    check('invalid IP budget reaches 429 with zero BE arrivals',429 in outcomes and len(requests)==before)
    check('valid user survives saturated invalid-IP quota',request(token=token(user='202'))[0]==200)
    check('signed revoked fixture still reaches BE rejection',request(token=token(user='203',sid='revoked-session'))[0]==401 and requests[-1]['status']==401)
    # Existing auth exceptions and write paths are kept. Fresh connections avoid carrying read bodies.
    for path in ['/api/auth/csrf','/api/auth/kakao/authorize','/api/auth/kakao/callback','/api/actuator/health']:
        check('auth exception '+path,request(path)[0]==200)
    check('POST trip body and creation path retained',request(token=None,method='POST',body='trip-data')[0]==200 and requests[-1]['bodyBytes']==9)
    check('POST upload path retained',request('/api/trips/1/initial-attachments',method='POST',body='photo-fixture')[0]==200 and requests[-1]['bodyBytes']==13)
    check('OPTIONS retained',request('/api/trips',method='OPTIONS')[0]==200)
    # Named locations are not public URIs. A URI spelling the name only reaches the ordinary FE.
    request('/@yeodam_jwt_user_api')
    check('caller cannot address internal user proxy by URL',requests[-1]['path']=='/@yeodam_jwt_user_api' and requests[-1]['method']=='GET' and requests[-1]['port']==3000)
    logs=[json.loads(line) for line in Path('/var/log/nginx/yeodam-access.json').read_text().splitlines()]
    from collections import Counter
    print('local response classes:',dict(Counter((x['status'],x['jwt_gate_result'],x['upstream_status']) for x in logs)))
    check('edge user and invalid rejects have no upstream',any(x['status']==429 and x['jwt_gate_result']=='signature_valid' and x['upstream_status'] in ('','-') for x in logs) and any(x['status']==429 and x['jwt_gate_result']=='missing' and x['upstream_status'] in ('','-') for x in logs))
    # JSON logs contain only classes. Never persist raw credentials or signed user identifiers.
    raw=Path('/var/log/nginx/yeodam-access.json').read_text()
    check('token and signing key absent from JSON logs',a not in raw and config['secret'] not in raw and 'u:200' not in raw)
    print(json.dumps({'checks':len(checks),'passed':checks,'scope':'local synthetic fixture; not real staging deployment/load','nginxPackages':subprocess.check_output(['dpkg-query','-W','-f=${Package} ${Version}\\n','nginx','libnginx-mod-http-js'],text=True)},ensure_ascii=False))
finally:
    if client:client.close()
    nginx.terminate()
    try:nginx.wait(timeout=5)
    except subprocess.TimeoutExpired:nginx.kill();nginx.wait()
    if nginx.returncode not in (0,-15): print(nginx.stderr.read().decode()[-3000:])
    for server in servers:server.shutdown()
