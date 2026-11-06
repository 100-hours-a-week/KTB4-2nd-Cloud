#!/usr/bin/env python3
"""Screen exact Git source for V3 connection capabilities. Not runtime or registry proof."""
import argparse,ast,hashlib,json,re,subprocess
from pathlib import Path
FILES=['app/jobs/config.py','app/core/config.py','app/jobs/kafka.py','app/infra/qdrant.py','app/infra/place_index.py','app/jobs/runner.py']
def inspect(files,source):
    trees={k:ast.parse(v) for k,v in files.items()};settings='\n'.join(files[k] for k in FILES[:2])
    annotations={n.target.id:ast.unparse(n.annotation) for k in FILES[:2] for n in ast.walk(trees[k]) if isinstance(n,ast.AnnAssign) and isinstance(n.target,ast.Name)}
    checks={'kafkaProtocol':'SASL_SSL' in annotations.get('KAFKA_SECURITY_PROTOCOL',''),'kafkaSettings':all(k in annotations for k in ['KAFKA_SASL_MECHANISM','KAFKA_SASL_USERNAME','KAFKA_SASL_PASSWORD','KAFKA_SSL_CAFILE'])}
    option=next((n for n in ast.walk(trees['app/jobs/kafka.py']) if isinstance(n,ast.FunctionDef) and n.name=='connection_options'),None)
    keys={n.value for n in ast.walk(option) if isinstance(n,ast.Constant) and isinstance(n.value,str)} if option else set()
    checks['kafkaClientOptions']={'ssl_context','sasl_mechanism','sasl_plain_username','sasl_plain_password'}<=keys
    checks['qdrantSetting']='QDRANT_API_KEY' in annotations
    calls=[n for n in ast.walk(trees['app/infra/qdrant.py']) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in ['QdrantClient','AsyncQdrantClient'] and any(k.arg=='url' for k in n.keywords)]
    checks['qdrantClients']=len(calls)>=2 and all(any(k.arg=='api_key' and not (isinstance(k.value,ast.Constant) and k.value.value is None) for k in n.keywords) for n in calls)
    checks['placeIndexSettings']=all(k in annotations for k in ['PLACE_INDEX_PATH','PLACE_INDEX_SHA256','PLACE_INDEX_S3_URI','KAKAO_REST_API_KEY'])
    checks['runnerIndexPreparation']=any(isinstance(n,ast.Name) and n.id=='prepare_place_index' for n in ast.walk(trees['app/jobs/runner.py']))
    return {'kind':'v3-ai-source-screen-v1','source':source,'status':'compatible' if all(checks.values()) else 'blocked','checks':checks,'missing':[k for k,v in checks.items() if not v],'files':{k:hashlib.sha256(v.encode()).hexdigest() for k,v in files.items()},'runtimeVerified':False}
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--repository',type=Path,required=True);p.add_argument('--source',required=True);p.add_argument('--output',type=Path);a=p.parse_args()
    if not re.fullmatch('[a-f0-9]{40}',a.source):raise SystemExit('Exact source Commit required')
    files={k:subprocess.check_output(['git','-C',str(a.repository),'show',a.source+':'+k],text=True) for k in FILES};result=inspect(files,a.source)
    if a.output:a.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'source':a.source,'status':result['status'],'missing':result['missing'],'runtimeVerified':False}))
    return 0 if result['status']=='compatible' else 1
if __name__=='__main__':raise SystemExit(main())
