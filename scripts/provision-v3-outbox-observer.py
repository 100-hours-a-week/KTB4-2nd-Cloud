#!/usr/bin/env python3
"""Explicit post-merge provisioning only; keep root secret in Pod and observer secret in memory."""
import argparse
import base64
import json
import re
import secrets
from v3_cdc_fault import Kube, require, ready_pod
TABLES=('trip_creation_outbox','additional_attachment_upload_outbox','story_generation_outbox','attachment_conversion_outbox')
def sql(password):
    require(bool(re.fullmatch('[a-f0-9]{48}',password)),'Generated observer password format required')
    statements=["CREATE USER IF NOT EXISTS 'cloud_outbox_observer'@'%' IDENTIFIED BY '"+password+"' REQUIRE SSL WITH MAX_USER_CONNECTIONS 1;", "ALTER USER 'cloud_outbox_observer'@'%' IDENTIFIED BY '"+password+"' REQUIRE SSL WITH MAX_USER_CONNECTIONS 1;", "REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'cloud_outbox_observer'@'%';"]
    return '\n'.join(statements+[f"GRANT SELECT (status,cdc_managed,created_at) ON yeodam.{t} TO 'cloud_outbox_observer'@'%';"for t in TABLES])+'\n'

def main():
    p=argparse.ArgumentParser();p.add_argument('--kubeconfig',required=True);p.add_argument('--mysql-pod-uid',required=True);p.add_argument('--execute-reviewed-provisioning',action='store_true');a=p.parse_args()
    require(a.execute_reviewed_provisioning,'Explicit post-merge secret/account provisioning approval required')
    k=Kube(a.kubeconfig);k.identity();pod=k.get('pod','mysql','mysql-0')
    require(pod['metadata']['uid']==a.mysql_pod_uid and ready_pod(pod),'Review exact primary Pod UID')
    # Existing fixed account/Secret only, no admin secret ever leaves the Pod.
    existing=json.loads(k.call(['-n','observability','get','secret','v3-outbox-observer','--ignore-not-found','-o','json']) or '{}')
    if existing:
        require(existing['metadata'].get('labels',{}).get('yeodam.io/owner')=='outbox-observer','Never adopt an unrelated Secret')
        password=base64.b64decode(existing['data']['password'],validate=True).decode()
    else:
        check="""source /runtime/common.sh; umask 077; c=$(mktemp /tmp/outbox-collision.XXXXXX); trap 'rm -f "$c"' EXIT; client_file root local "$c" root; mysql --defaults-extra-file="$c" --batch --skip-column-names --silent 2>/dev/null"""
        present=k.call(['-n','mysql','exec','-i','mysql-0','-c','mysql','--','/bin/bash','-ec',check],"SELECT COUNT(*) FROM mysql.user WHERE User='cloud_outbox_observer';\n")
        require(present.strip()=='0','Existing DB account without owned Secret; explicit recovery review required')
        password=secrets.token_hex(24)
    ca=k.call(['-n','mysql','exec','mysql-0','-c','mysql','--','cat','/tls/ca.crt'])
    require('BEGIN CERTIFICATE'in ca and 'PRIVATE KEY'not in ca,'Public CA only')
    client='[client]\nuser=cloud_outbox_observer\npassword='+password+'\nhost=mysql-0.mysql-members.mysql.svc.cluster.local\nport=3306\nprotocol=TCP\nssl-mode=VERIFY_IDENTITY\nssl-ca=/outbox-auth/ca.crt\nconnect-timeout=3\n'
    # Persist/reuse the new observer credential before SQL so a partial setup is retryable.
    secret={'apiVersion':'v1','kind':'Secret','metadata':{'name':'v3-outbox-observer','namespace':'observability','labels':{'yeodam.io/owner':'outbox-observer'}},'type':'Opaque','data':{n:base64.b64encode(v.encode()).decode()for n,v in [('client.cnf',client),('ca.crt',ca),('password',password)]}}
    if not existing:k.call(['create','-f','-'],json.dumps(secret))
    command="""source /runtime/common.sh; umask 077; c=$(mktemp /tmp/outbox-account.XXXXXX); trap 'rm -f "$c"' EXIT; client_file root local "$c" root; state=$(printf 'SELECT NOT @@global.read_only AND NOT @@global.super_read_only;\n' | mysql --defaults-extra-file="$c" --batch --skip-column-names --silent 2>/dev/null); [[ $state == 1 ]] || exit 1; mysql --defaults-extra-file="$c" --batch --silent >/dev/null 2>/dev/null"""
    k.call(['-n','mysql','exec','-i','mysql-0','-c','mysql','--','/bin/bash','-ec',command],sql(password))
    print(json.dumps({'observerAccountReady':True,'secret':'observability/v3-outbox-observer','tables':len(TABLES),'appChanges':False}))
if __name__=='__main__':main()
