#!/usr/bin/env python3
"""Fixed Prometheus engine checks, synthetic counters only; no AWS/cluster/GPU."""
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import yaml

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('experiment',ROOT/'scripts/collect-v3-experiment.py')
e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e)
image=json.loads((ROOT/'deploy/v3/metrics/images.lock.json').read_text())['prometheus']
expressions={}
class Queries:
    def query(self,expr,start,end):
        expressions[str(len(expressions))]=expr
        return {'status':'missing','series':[]}
e.counts_and_percentiles(Queries(),0,600)
queries=list(expressions.values())
rules={'groups':[{'name':'experiment-syntax','rules':[{'record':'fixture_'+str(i),'expr':q} for i,q in enumerate([*e.RANGE.values(),*queries])]}]}
series=[
 {'series':'http_server_requests_seconds_count{job="backend",workload="trip-api",uri="/api/trips",status="200"}','values':'0+1x20'},
 {'series':'http_server_requests_seconds_count{job="backend",workload="trip-api",uri="/actuator/health",status="200"}','values':'0+100x20'},
 {'series':'http_server_requests_seconds_bucket{job="backend",workload="trip-api",uri="/api/trips",le="0.5"}','values':'0+1x20'},
 {'series':'http_server_requests_seconds_bucket{job="backend",workload="trip-api",uri="/api/trips",le="+Inf"}','values':'0+2x20'},
 {'series':'yeodam_ai_jobs{job="ai",instance="a",state="queued_process"}','values':'4+0x20'},
 {'series':'yeodam_ai_jobs{job="ai",instance="b",state="queued_process"}','values':'4+0x20'},
]
checks=[
 {'expr':queries[0],'eval_time':'10m','exp_samples':[{'labels':'{workload="trip-api"}','value':20}]},
 {'expr':queries[1],'eval_time':'10m','exp_samples':[{'labels':'{workload="trip-api"}','value':0}]},
 {'expr':queries[2],'eval_time':'10m','exp_samples':[{'labels':'{workload="trip-api"}','value':.5}]},
 {'expr':e.RANGE['ai_jobs'],'eval_time':'10m','exp_samples':[{'labels':'{state="queued_process"}','value':4}]},
 {'expr':e.RANGE['cdc_probe'],'eval_time':'10m','exp_samples':[]},
]
# Reset-aware increase is an estimate, not an exact DB count.
series.append({'series':'http_server_requests_seconds_count{job="backend",workload="photo-api",uri="/api/photos",status="200"}','values':'0+1x9 0+1x10'})
series.extend([
 {'series':'yeodam_outbox_probe_success','values':'1+0x19 0'},
 {'series':'yeodam_outbox_pending{outbox="photo"}','values':'8+0x20'},
 {'series':'yeodam_outbox_count_capped{outbox="photo"}','values':'0+0x20'},
 *[{'series':f'keda_scaler_metrics_value{{scaledObject="photo-converter",scalerIndex="{i}",metric="lane{i}"}}','values':'0+0x20'}for i in range(4)],
])
checks.extend([
 {'expr':e.RANGE['outbox_probe'],'eval_time':'10m','exp_samples':[{'labels':'yeodam_outbox_probe_success','value':0}]},
 {'expr':e.RANGE['outbox_pending_raw'],'eval_time':'10m','exp_samples':[{'labels':'yeodam_outbox_pending{outbox="photo"}','value':8}]},
 {'expr':e.RANGE['outbox_count_capped'],'eval_time':'10m','exp_samples':[{'labels':'yeodam_outbox_count_capped{outbox="photo"}','value':0}]},
 {'expr':e.RANGE['conversion_lag'],'eval_time':'10m','exp_samples':[{'labels':f'keda_scaler_metrics_value{{scaledObject="photo-converter",scalerIndex="{i}",metric="lane{i}"}}','value':0}for i in range(4)]},
])
# Keep reset measurement separate so trip-only expected samples stay exact.
checks[0]['exp_samples'].append({'labels':'{workload="photo-api"}','value':18.94736842105263})
checks[1]['exp_samples'].append({'labels':'{workload="photo-api"}','value':0})
fixture={'rule_files':['rules.yaml'],'evaluation_interval':'30s','tests':[{'interval':'30s','input_series':series,'promql_expr_test':checks}]}
with tempfile.TemporaryDirectory(prefix='645-promql-') as d:
    p=Path(d);(p/'rules.yaml').write_text(yaml.safe_dump(rules));(p/'test.yaml').write_text(yaml.safe_dump(fixture))
    target='linux/arm64' if platform.system()=='Darwin' and platform.machine()=='arm64' else 'linux/amd64'
    command=['docker','run','--rm','--platform',target,'--network','none','--user',str(os.getuid())+':'+str(os.getgid()),'--read-only','--tmpfs','/tmp:size=32m,mode=1777','--cap-drop','ALL','--memory','128m','--cpus','.5','-v',str(p)+':/fixture:ro','-w','/fixture','--entrypoint','/bin/promtool',image,'test','rules','test.yaml']
    subprocess.run(command,check=True,timeout=60)
print(json.dumps({'passed':True,'querySyntax':len(rules['groups'][0]['rules']),'checks':['health excluded','zero errors with traffic','whole-window histogram p95','shared AI queue not summed twice','missing CDC unknown','counter reset estimate','Outbox backlog and four zero-lag lanes coexist','failed Outbox probe preserved separately from raw count','count cap retained'],'aws':False}))
