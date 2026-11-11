"""Synthetic container-credential + Logs Insights server, never an AWS proxy."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

GROUPS = ['/yeodam/v3/production/ai-api', '/yeodam/v3/production/ai-runner', '/yeodam/v3/production/backend']
counts = {'credentials': 0, 'StartQuery': 0, 'GetQueryResults': 0}
class Handler(BaseHTTPRequestHandler):
    def reply(self, data, status=200):
        raw=json.dumps(data).encode();self.send_response(status);self.send_header('Content-Type','application/x-amz-json-1.1');self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if self.path == '/credentials':
            assert self.headers.get('Authorization') == 'synthetic-fixture-only'
            counts['credentials']+=1
            self.reply({'AccessKeyId':'synthetic-fixture-only','SecretAccessKey':'synthetic-fixture-only','Token':'synthetic-fixture-only','Expiration':'2030-01-01T00:00:00Z'})
        elif self.path == '/proof':self.reply(counts)
        else:self.reply({},404)
    def do_POST(self):
        assert 'Credential=synthetic-fixture-only/' in self.headers.get('Authorization','')
        assert self.headers.get('X-Amz-Security-Token') == 'synthetic-fixture-only'
        action=self.headers.get('X-Amz-Target','').split('.')[-1] or self.path.rsplit('/',1)[-1]
        raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
        # New CloudWatch SDK uses Smithy RPCv2 CBOR; Logs uses JSON.
        if action == 'ListMetrics' and 'cbor' in self.headers.get('Content-Type',''):
            counts['ListMetrics']=counts.get('ListMetrics',0)+1
            self.send_response(200);self.send_header('Content-Type','application/cbor');self.send_header('smithy-protocol','rpc-v2-cbor');self.end_headers();self.wfile.write(b'\xa0');return
        data=json.loads(raw or b'{}')
        if action == 'StartQuery':
            groups=data.get('logGroupNames',data.get('logGroupIdentifiers',[]))
            allowed=GROUPS+['arn:aws:logs:ap-northeast-2:483175530259:log-group:'+g for g in GROUPS]
            assert groups and all(g in allowed for g in groups)
            counts[action]+=1;self.reply({'queryId':'00000000-0000-4000-8000-000000000001'})
        elif action == 'GetQueryResults':
            counts[action]+=1
            self.reply({'status':'Complete','results':[[{'field':'@timestamp','value':'2026-11-11 00:00:00.000'},{'field':'app.job_id','value':'fixture-job'},{'field':'app.trace_id','value':'c'*32},{'field':'@message','value':'synthetic redacted application event'}]],'statistics':{'recordsMatched':1,'recordsScanned':1,'bytesScanned':128}})
        elif action == 'ListMetrics':
            counts['ListMetrics']=counts.get('ListMetrics',0)+1;self.reply({'Metrics':[]})
        elif action == 'DescribeLogGroups':self.reply({'logGroups':[{'logGroupName':g,'arn':'arn:aws:logs:ap-northeast-2:483175530259:log-group:'+g+':*'} for g in GROUPS]})
        elif action == 'GetLogGroupFields':self.reply({'logGroupFields':[{'name':'app.job_id','percent':100},{'name':'app.trace_id','percent':100}]})
        else:self.reply({'__type':'AccessDeniedException','message':'Synthetic fixture denies unsupported actions'},403)
    def log_message(self,*args):pass
HTTPServer(('0.0.0.0',8085),Handler).serve_forever()
