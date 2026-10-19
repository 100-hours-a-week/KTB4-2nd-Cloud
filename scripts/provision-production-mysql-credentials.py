#!/usr/bin/env python3
"""Initialize only missing production DB credentials; never print or rotate values."""
import json,secrets,subprocess,tempfile,os
base=['aws','--profile','yeodam-admin','--region','ap-northeast-2']
def write_request(args,request):
    with tempfile.NamedTemporaryFile(mode='w',prefix='credential-',suffix='.json') as f:
        os.chmod(f.name,0o600)
        json.dump(request,f); f.flush()
        return subprocess.run(base+args+['--cli-input-json','file://'+f.name,'--output','json'],capture_output=True,text=True)

account=subprocess.check_output(base+['sts','get-caller-identity','--query','Account','--output','text'],text=True).strip()
if account!='483175530259': raise SystemExit('Wrong AWS account; no credential changed')
secret='yeodam-v2-production/mysql-root-password'
p=subprocess.run(base+['secretsmanager','describe-secret','--secret-id',secret,'--output','json'],capture_output=True,text=True)
if p.returncode: raise SystemExit('Secret metadata is not ready; no value changed')
meta=json.loads(p.stdout)
if not any('AWSCURRENT' in stages for stages in meta.get('VersionIdsToStages',{}).values()):
    request={'SecretId':secret,'SecretString':secrets.token_hex(32)}
    r=write_request(['secretsmanager','put-secret-value'],request)
    if r.returncode: raise SystemExit('Root credential write failed: '+r.stderr.replace(request['SecretString'],'[REDACTED]')[:700])
    print('Root credential initialized in the new production secret; value omitted')
else: print('Existing production root credential preserved')
name='/yeodam/v2/production/app/MYSQL_PASSWORD'
p=subprocess.run(base+['ssm','get-parameter','--name',name,'--output','json'],capture_output=True,text=True)
if p.returncode==0: print('Existing production app credential preserved')
elif 'ParameterNotFound' in p.stderr:
    request={'Name':name,'Type':'SecureString','KeyId':'alias/aws/ssm','Value':secrets.token_hex(32)}
    r=write_request(['ssm','put-parameter'],request)
    if r.returncode: raise SystemExit('App credential write failed: '+r.stderr.replace(request['Value'],'[REDACTED]')[:700])
    print('App credential initialized in production Parameter Store; value omitted')
else: raise SystemExit('App parameter lookup failed; no parameter changed')
