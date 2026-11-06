#!/usr/bin/env python3
"""Generate new V3-only bootstrap files privately. No AWS, cluster or existing credentials."""
import argparse,json,os,secrets,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def prepare(directory):
    directory=directory.resolve()
    if directory.is_relative_to(ROOT):raise ValueError('Generated secrets must stay outside this checkout')
    os.umask(0o077);directory.mkdir(parents=True,exist_ok=False)
    for name in ['ai-redis','qdrant']:
        d=directory/name;d.mkdir()
        def openssl(*args):
            r=subprocess.run(['openssl',*map(str,args)],cwd=d,capture_output=True)
            if r.returncode:raise RuntimeError('TLS generation failed; generated private files retained for review')
        (d/'ca.cnf').write_text('[req]\ndistinguished_name=dn\nx509_extensions=v3_ca\n[dn]\n[v3_ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n')
        openssl('req','-x509','-newkey','rsa:2048','-nodes','-sha256','-days','365','-config','ca.cnf','-subj','/CN=yeodam-v3-'+name+'-ca','-keyout','ca.key','-out','ca.crt')
        openssl('req','-new','-newkey','rsa:2048','-nodes','-subj','/CN='+name+'.'+name+'.svc.cluster.local','-keyout','tls.key','-out','server.csr')
        dns=[name,name+'.'+name,name+'.'+name+'.svc',name+'.'+name+'.svc.cluster.local',name+'-0.'+name+'-members.'+name+'.svc.cluster.local']
        (d/'extensions.cnf').write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\nsubjectAltName='+','.join('DNS:'+s for s in dns)+',IP:127.0.0.1\n')
        openssl('x509','-req','-in','server.csr','-CA','ca.crt','-CAkey','ca.key','-CAcreateserial','-days','90','-sha256','-extfile','extensions.cnf','-out','tls.crt')
        payload={'tlsCa':(d/'ca.crt').read_text(),'tlsCert':(d/'tls.crt').read_text(),'tlsKey':(d/'tls.key').read_text()}
        payload.update({k:secrets.token_hex(32) for k in (['appPassword','adminPassword','healthPassword'] if name=='ai-redis' else ['apiKey'])})
        (d/'source.json').write_text(json.dumps(payload,indent=2)+'\n')
        for f in d.iterdir():f.chmod(0o600)
    print('Generated new V3 bootstrap files only. No AWS or cluster changes; never commit or print payloads.')
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',required=True,type=Path);prepare(p.parse_args().directory)
