#!/usr/bin/env python3
"""Create a public-CA-only Helm overlay. Never export bootstrap credentials or private keys."""
import argparse,base64,hashlib,json,os,subprocess,tempfile
from pathlib import Path
from v3_outbox_contract import public_jks,require

def prepare(pem,output):
 require('PRIVATE KEY' not in pem and pem.count('-----BEGIN CERTIFICATE-----')==1 and pem.count('-----END CERTIFICATE-----')==1,'Only one public CA certificate is allowed')
 require(not output.exists(),'Refusing to replace an existing deployment input')
 with tempfile.TemporaryDirectory(prefix='v3-outbox-public-ca-') as d:
  ca=Path(d)/'ca.crt';store=Path(d)/'mysql.jks';ca.write_text(pem)
  r=subprocess.run(['keytool','-importcert','-noprompt','-alias','v3-mysql-ca','-file',str(ca),'-keystore',str(store),'-storetype','JKS','-storepass','public-ca-only','-J-Xmx64m'],capture_output=True,text=True,timeout=60)
  require(r.returncode==0,'Public CA import failed; no credential material exported')
  encoded=base64.b64encode(store.read_bytes()).decode();public_jks(encoded)
  output.parent.mkdir(parents=True,exist_ok=True)
  with output.open('x') as f:json.dump({'enabled':True,'connectorEnabled':False,'mysqlTruststore':encoded},f,indent=2)
  output.chmod(0o600)
 print('Prepared public MySQL CA overlay; no credentials or private keys included')
def main():
 p=argparse.ArgumentParser(description=__doc__);g=p.add_mutually_exclusive_group(required=True);g.add_argument('--ca-file',type=Path);g.add_argument('--read-v3-ca',action='store_true');p.add_argument('--output',required=True,type=Path);p.add_argument('--profile',default='yeodam-admin');a=p.parse_args();os.umask(0o077)
 if a.ca_file:pem=a.ca_file.read_text()
 else:
  aws=['aws','--profile',a.profile,'--region','ap-northeast-2']
  def call(args):
   r=subprocess.run(aws+args,capture_output=True,text=True,timeout=60);require(r.returncode==0,'V3 CA lookup failed; output withheld');return json.loads(r.stdout)
  require(call(['sts','get-caller-identity','--output','json'])['Account']=='483175530259','Wrong AWS account')
  # Operator-only bootstrap lookup. Select only tlsCa; Kafka reader never gets this source ARN.
  payload=json.loads(call(['secretsmanager','get-secret-value','--secret-id','yeodam/v3/foundation/mysql/bootstrap','--output','json'])['SecretString']);pem=payload['tlsCa'];del payload
 prepare(pem,a.output)
if __name__=='__main__':main()
