#!/usr/bin/env python3
"""Explicit V3-only CDC account operations; no remote root, reset, rotation, or value output."""
import argparse,json,os,re,secrets,subprocess,tempfile,time
from urllib.parse import urlparse
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
LOCK=json.loads((ROOT/'deploy/v3/mysql-schema-image/source.lock.json').read_text())
NAME='yeodam/v3/foundation/mysql/cdc';USER='v3_connect_cdc';CLUSTER='yeodam-v3-foundation'
TABLES=LOCK['cdcTables']
GLOBAL={'RELOAD','SHOW DATABASES','REPLICATION SLAVE','REPLICATION CLIENT'}

def require(value,message='V3 CDC precondition failed'):
 if not value:raise ValueError(message)

def payload():return {'username':USER,'password':secrets.token_hex(24)}
def create_sql(password):
 if not re.fullmatch('[a-f0-9]{48}',password):raise ValueError('Invalid new CDC credential format')
 return f"CREATE USER '{USER}'@'%' IDENTIFIED BY '{password}' REQUIRE SSL WITH MAX_USER_CONNECTIONS 4;\nGRANT RELOAD, SHOW DATABASES, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO '{USER}'@'%';\n"+''.join(f"GRANT SELECT ON yeodam.{t} TO '{USER}'@'%';\n" for t in TABLES)

def grants_match(lines):
 expected={'*.*':GLOBAL,**{'yeodam.'+t:{'SELECT'} for t in TABLES}};seen={}
 for line in lines.splitlines():
  line=line.replace('`','')
  m=re.fullmatch(r"GRANT (.+) ON ([a-z_.*]+) TO (?:'v3_connect_cdc'|v3_connect_cdc)@(?:'%'|%)",line)
  if not m or m[2] in seen:return False
  seen[m[2]]=set(m[1].split(', '))
 return seen==expected

ROOT_SHELL="""set -Eeuo pipefail
source /runtime/common.sh
f=/tmp/cdc-root-$$.cnf
trap 'rm -f "$f" /tmp/cdc-error-$$' EXIT
client_file root local "$f" root
mysql --defaults-extra-file="$f" --batch --skip-column-names --silent 2>/tmp/cdc-error-$$
"""
AUTH_SHELL="""set -Eeuo pipefail
umask 077
read -r cdc_password
[[ $cdc_password =~ ^[a-f0-9]{48}$ ]]
f=/tmp/cdc-auth-$$.cnf
trap 'rm -f "$f" /tmp/cdc-error-$$' EXIT
printf '[client]\\nuser=v3_connect_cdc\\npassword=%s\\nhost=%s\\nprotocol=TCP\\nssl-mode=VERIFY_IDENTITY\\nssl-ca=/tls/ca.crt\\nconnect-timeout=5\\n' "$cdc_password" "$POD_NAME.mysql-members.mysql.svc.cluster.local" > "$f"
unset cdc_password
mysql --defaults-extra-file="$f" --batch --skip-column-names --silent -e 'SELECT 1; SHOW SESSION STATUS LIKE "Ssl_cipher";' 2>/tmp/cdc-error-$$
"""

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['create-secret','create-account','verify-account']);p.add_argument('--profile',default='yeodam-admin');p.add_argument('--kubeconfig',type=Path);a=p.parse_args();os.umask(0o077)
 def execute(command,source=None):
  r=subprocess.run(command,input=source,capture_output=True,text=True,timeout=90)
  if r.returncode:raise RuntimeError('Controlled operation failed; output withheld to protect credentials')
  return r.stdout.strip()
 aws=['aws','--profile',a.profile,'--region','ap-northeast-2']
 identity=json.loads(execute(aws+['sts','get-caller-identity','--output','json']));require(identity['Account']=='483175530259','Wrong AWS account')
 if a.action=='create-secret':
  check=subprocess.run(aws+['secretsmanager','describe-secret','--secret-id',NAME],capture_output=True,text=True)
  if check.returncode==0:raise RuntimeError('CDC Secret exists; refusing overwrite/rotation')
  if 'ResourceNotFoundException' not in check.stderr:raise RuntimeError('Secret lookup failed')
  with tempfile.TemporaryDirectory(prefix='v3-mysql-cdc-') as d:
   f=Path(d)/'payload.json';f.write_text(json.dumps(payload()));f.chmod(0o600)
   execute(aws+['secretsmanager','create-secret','--name',NAME,'--secret-string','file://'+str(f),'--description','V3 MySQL CDC account; explicit create only','--tags','Key=Project,Value=yeodam','Key=Version,Value=v3','Key=Issue,Value=540'])
  print('Created new V3 CDC Secret; no values printed');return
 if not a.kubeconfig:raise ValueError('Explicit V3 kubeconfig required')
 kube=['kubectl','--kubeconfig',str(a.kubeconfig),'--request-timeout=30s']
 context=execute(kube+['config','current-context']);require(context==CLUSTER or context.endswith(':cluster/'+CLUSTER),'Wrong cluster context')
 cluster=json.loads(execute(aws+['eks','describe-cluster','--name',CLUSTER,'--output','json']))['cluster']
 require(cluster['status']=='ACTIVE' and cluster['resourcesVpcConfig']['vpcId']=='vpc-0ecc3132f3b2170e9','Wrong EKS identity')
 cfg=json.loads(execute(kube+['config','view','--minify','-o','json']))['clusters'][0]['cluster']
 require(cfg['server']==cluster['endpoint'] or (urlparse(cfg['server']).hostname=='127.0.0.1' and cfg.get('tls-server-name','').lower()==urlparse(cluster['endpoint']).hostname.lower()),'Kubeconfig endpoint does not match verified EKS')
 require(not cfg.get('insecure-skip-tls-verify',False),'TLS bypass rejected')
 for member in ['mysql-0','mysql-1']:
  pod=json.loads(execute(kube+['get','pod',member,'-n','mysql','-o','json']));labels=pod['metadata']['labels']
  require(labels.get('yeodam.io/stage')=='v3' and labels.get('app.kubernetes.io/instance')=='yeodam-v3-mysql')
  require(all(c['ready'] for c in pod['status']['containerStatuses']))
 def sql(member,source):return execute(kube+['exec','-i','-n','mysql',member,'--','/bin/bash','-ec',ROOT_SHELL],source)
 require(sql('mysql-0','SELECT NOT @@global.read_only AND NOT @@global.super_read_only;')=='1','Expected mysql-0 writable primary')
 require(sql('mysql-1','SELECT @@global.read_only AND @@global.super_read_only;')=='1','Expected mysql-1 read-only replica')
 for table in TABLES:
  require(sql('mysql-0',f"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='yeodam' AND table_name='{table}';")=='1','Required Outbox schema absent')
 history="SELECT CONCAT(COUNT(*),':',SUM(success),':',COUNT(DISTINCT version),':',MAX(CAST(version AS UNSIGNED))) FROM yeodam.flyway_schema_history;"
 for member in ['mysql-0','mysql-1']:
  require(sql(member,history)=='34:34:34:34','Both schema histories must reach exactly V34 without failed rows')
  require(sql(member,'SELECT script FROM yeodam.flyway_schema_history ORDER BY installed_rank;')=='\n'.join(x['name'] for x in LOCK['migrations']),'Migration script set differs')
 raw=json.loads(execute(aws+['secretsmanager','get-secret-value','--secret-id',NAME,'--output','json']));auth=json.loads(raw['SecretString'])
 require(auth['username']==USER and re.fullmatch('[a-f0-9]{48}',auth['password']))
 exists=f"SELECT COUNT(*) FROM mysql.user WHERE user='{USER}';"
 if a.action=='create-account':
  require(sql('mysql-0',exists)==sql('mysql-1',exists)=='0','CDC account exists or topology diverged; refusing overwrite/automatic retry')
  sql('mysql-0',create_sql(auth['password']))
  for _ in range(30):
   if sql('mysql-1',exists)=='1':break
   time.sleep(1)
 for member in ['mysql-0','mysql-1']:
  require(sql(member,exists)=='1')
  check=f"SELECT COUNT(*) FROM mysql.user WHERE user='{USER}' AND host='%' AND ssl_type='ANY' AND plugin='caching_sha2_password' AND max_user_connections=4;"
  require(sql(member,check)=='1','CDC authentication restriction differs')
  require(grants_match(sql(member,f"SHOW GRANTS FOR '{USER}'@'%';")),'CDC grant scope differs')
  login=execute(kube+['exec','-i','-n','mysql',member,'--','/bin/bash','-ec',AUTH_SHELL],auth['password']+'\n')
  require(login.startswith('1\nSsl_cipher\t') and len(login.split('\t')[-1])>3,'CDC TLS authentication failed')
 print('PASS: CDC account on primary/replica, TLS auth, four Outbox SELECT grants and exact replication/snapshot privileges; no overwrite')
if __name__=='__main__':main()
