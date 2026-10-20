#!/usr/bin/env python3
"""Host-local actions for the isolated #376 migration rehearsal."""
import argparse
import json
from pathlib import Path
import subprocess
import urllib.request

IPS={'source':'10.30.0.174','target':'10.30.10.28','kafka':'10.30.10.95','generator':'10.30.0.126'}

def shell(command):
    p=subprocess.run(command,capture_output=True,text=True)
    if p.returncode:raise RuntimeError('Host action failed: '+command[0])
    return p.stdout.strip()

def role():
    addresses=shell(['hostname','-I']).split()
    found=[k for k,v in IPS.items() if v in addresses]
    if len(found)!=1:raise RuntimeError('Not an approved migration host')
    return found[0]

def api(path):
    with urllib.request.urlopen('http://127.0.0.1:8083'+path,timeout=10) as r:return json.load(r)

def inventory():
    host=role();print(json.dumps({'host':host,'memory':shell(['free','-m']),'disk':shell(['df','-h','/'])}))
    if host=='kafka':
        for name in api('/connectors'):
            s=api('/connectors/'+name+'/status')
            print(json.dumps({'connector':name,'state':s['connector']['state'],'tasks':[t['state'] for t in s['tasks']]}))
        return
    print(shell(['docker','ps','--format','{{.Names}} {{.Status}}']) if host in ('source','target') else shell(['pgrep','-c','k6']) if subprocess.run(['pgrep','k6'],capture_output=True).returncode==0 else 'k6_count=0')
    if host=='source':
        print(json.dumps({'authPaths':[str(p) for p in Path('/run/yeodam-cdc-auth368').glob('*')],
                          'aPaths':[str(p) for p in Path('/var/lib/yeodam/a-rehearsal-358').glob('*')],
                          'authRoot':[str(p) for p in Path('/opt/yeodam-cdc-auth368').glob('*')]}))
        for base in ('/opt/yeodam-cdc-auth368/venv/bin/python3','/var/lib/yeodam/a-rehearsal-358/venv/bin/python3'):
            if Path(base).exists():
                p=subprocess.run([base,'-c',"import importlib.util,json;print(json.dumps({m:importlib.util.find_spec(m) is not None for m in ['redis','pymysql','confluent_kafka','cryptography']}))"],capture_output=True,text=True)
                print(base+': '+p.stdout.strip())

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('inventory',));a=p.parse_args();inventory()
