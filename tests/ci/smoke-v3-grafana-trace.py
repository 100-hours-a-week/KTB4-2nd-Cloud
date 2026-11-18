#!/usr/bin/env python3
"""Same pinned Grafana image, CPU250m/memory256Mi: baseline versus unused-backend exclusion."""
import argparse,importlib.util,json,os,subprocess,tempfile,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('chart',ROOT/'scripts/check-v3-metrics.py');chart=importlib.util.module_from_spec(spec);spec.loader.exec_module(chart)
IMAGES=json.loads((ROOT/'deploy/v3/metrics/images.lock.json').read_text());APM=json.loads((ROOT/'deploy/v3/apm/images.lock.json').read_text())
PLATFORM='linux/amd64';prefix='grafana-trace-'+uuid.uuid4().hex[:8];names=[]
out=Path(os.environ.get('V3_GRAFANA_RESULTS','/tmp/'+prefix));out.mkdir(parents=True,exist_ok=True)
def run(*args,timeout=150):
    p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if p.returncode:raise RuntimeError((p.stdout+p.stderr)[-1600:])
    return p.stdout

def container(role,image,args=(),mounts=(),env=(),memory='256m',cpu='.25',aliases=()):
    name=prefix+'-'+role;names.append(name)
    cmd=['docker','run','-d','--platform',PLATFORM,'--name',name,'--network',prefix,'--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory',memory,'--memory-swap',memory,'--cpus',cpu]
    for alias in aliases:cmd+=['--network-alias',alias]
    for host,target,mode in mounts:cmd+=['-v',str(host)+':'+target+':'+mode]
    for value in env:cmd+=['-e',value]
    if role.startswith('grafana'):cmd+=['--user','472:472','--tmpfs','/var/lib/grafana:uid=472,gid=472,mode=0700,size=134217728','--tmpfs','/tmp:uid=472,gid=472,size=16777216']
    elif role=='tempo':cmd+=['--user','10001:10001','--tmpfs','/var/tempo:uid=10001,gid=10001,mode=0700']
    else:cmd+=['--user','65534:65534']
    run(*cmd,image,*args);return name

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--skip-baseline',action='store_true');args=parser.parse_args();proof={'platform':PLATFORM,'syntheticOnly':True,'aws':False,'profiles':{}}
    with tempfile.TemporaryDirectory(prefix=prefix)as tmp:
        root=Path(tmp);sdk=root/'sdk';state=root/'state';state.mkdir();state.chmod(0o777)
        extracted=prefix+'-sdk'
        try:run('docker','create','--platform',PLATFORM,'--name',extracted,APM['python']);run('docker','cp',extracted+':/autoinstrumentation',str(sdk))
        finally:subprocess.run(['docker','rm','-f',extracted],capture_output=True)
        run('docker','network','create','--internal',prefix)
        config=(ROOT/'deploy/v3/apm/files/tempo.yaml').read_text().replace("grpc: {endpoint: '0.0.0.0:4317'}","grpc: {endpoint: '0.0.0.0:4317'}\n        http: {endpoint: '0.0.0.0:4318'}")
        (root/'tempo.yaml').write_text(config)
        container('tempo',APM['tempo'],['-target=all','-config.file=/etc/tempo.yaml'],[(root/'tempo.yaml','/etc/tempo.yaml','ro')],['GOMEMLIMIT=256MiB','GOGC=50'],memory='384m',cpu='.5',aliases=['v3-tempo.observability.svc'])
        docs=chart.render(apm=True,logs=True)
        grafana=next(x for x in docs if x['kind']=='Deployment'and x['metadata']['name']=='v3-grafana')['spec']['template']['spec']['containers'][0]
        import yaml
        source=next(x for x in docs if x['kind']=='ConfigMap'and x['metadata']['name']=='v3-grafana-config')['data']['datasource.yaml']
        # No external datasource request: only local Tempo is provisioned here. The existing
        # metrics runtime separately exercises Prometheus and CloudWatch with a fake SDK endpoint.
        provision=yaml.safe_load(source);provision['datasources']=[x for x in provision['datasources']if x['uid']=='v3-tempo'];provision['datasources'][0]['url']='http://v3-tempo.observability.svc:3200'
        (root/'datasource.yaml').write_text(yaml.safe_dump(provision))
        fixture_mounts=[(sdk,'/otel','ro'),(ROOT/'tests/ci/v3/apm','/fixture','ro'),(ROOT/'scripts','/evidence','ro'),(state,'/state','rw')]
        fixture_env=['PYTHONPATH=/otel','PYTHONDONTWRITEBYTECODE=1','PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python']
        seed=container('seed',IMAGES['cdc-status'],['python','-B','/fixture/grafana-trace-fixture.py','seed'],fixture_mounts,fixture_env,memory='128m',cpu='.5')
        code=run('docker','wait',seed,timeout=130).strip();assert code=='0',run('docker','logs',seed)
        profiles=['baseline','bounded']if not args.skip_baseline else['bounded']
        for profile in profiles:
            values=[x['name']+'='+x['value']for x in grafana['env']if profile=='bounded'or x['name']!='GF_PLUGINS_DISABLE_PLUGINS']
            name=container('grafana-'+profile,IMAGES['grafana'],mounts=[(root/'datasource.yaml','/etc/grafana/provisioning/datasources/default.yaml','ro')],env=values,aliases=['v3-grafana'])
            probe=container('query-'+profile,IMAGES['cdc-status'],['python','-B','/fixture/grafana-trace-fixture.py','query'],fixture_mounts,fixture_env+['EXPECTED_DISABLED='+(','.join(sorted(chart.GRAFANA_DISABLED_PLUGINS))if profile=='bounded'else'')],memory='128m',cpu='.5')
            samples=[];deadline=time.monotonic()+250
            while time.monotonic()<deadline:
                s=json.loads(run('docker','inspect','--format','{{json .State}}',probe))
                metric=subprocess.run(['docker','exec',name,'cat','/sys/fs/cgroup/memory.current'],capture_output=True,text=True,timeout=5)
                if metric.returncode==0:samples.append(int(metric.stdout.strip()))
                if not s['Running']:break
                time.sleep(1)
            else:raise AssertionError('Synthetic Grafana query exceeded total budget')
            logs=run('docker','logs',probe);status=json.loads(run('docker','inspect','--format','{{json .State}}',name))
            process_count=subprocess.run(['docker','exec',name,'sh','-c','for d in /proc/[0-9]*/exe; do readlink "$d" 2>/dev/null; done'],capture_output=True,text=True,timeout=5)
            count=sum('/plugins-bundled/'in line for line in process_count.stdout.splitlines())
            row={'queryExitCode':s['ExitCode'],'grafanaOomKilled':status['OOMKilled'],'grafanaRunning':status['Running'],'backendProcesses':count,'memoryLimitBytes':268435456,'cpuLimitCores':.25,'memoryMaxSampleBytes':max(samples)if samples else None,'memorySamples':samples}
            if s['ExitCode']==0:row['query']=json.loads(logs.strip().splitlines()[-1])
            else:row['queryFailure']=logs[-1800:]
            proof['profiles'][profile]=row
            (out/'comparison.json').write_text(json.dumps(proof,indent=2))
            if profile=='bounded':
                assert s['ExitCode']==0 and status['Running']and not status['OOMKilled'],row
                assert count==3,'Only reviewed datasource backends may run'
            print(json.dumps({'profile':profile,**{k:v for k,v in row.items()if k not in ['memorySamples','queryFailure']}}),flush=True)
            for n in [probe,name]:
                logs=subprocess.run(['docker','logs',n],capture_output=True,text=True);(out/(n+'.log')).write_text(logs.stdout+logs.stderr)
                run('docker','rm','-f',n);names.remove(n)
        print('Synthetic comparison: '+str(out),flush=True)
try:main()
finally:
    for name in names:
        p=subprocess.run(['docker','logs',name],capture_output=True,text=True);(out/(name+'.log')).write_text(p.stdout+p.stderr)
        subprocess.run(['docker','rm','-f',name],capture_output=True)
    subprocess.run(['docker','network','rm',prefix],capture_output=True)
