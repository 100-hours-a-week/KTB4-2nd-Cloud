import json,pathlib
import sys
src=pathlib.Path(__file__).resolve().parents[2]/'monitoring/v2-production/alerts.json.tftpl';specs=json.loads(src.read_text());out=pathlib.Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True)
uids={'v2p-connector-missing','v2p-outbox-stuck','v2p-observer-failed','v2p-host-logs-stale'}
rules=[{'alert':s['uid'],'expr':'('+s['expr']+') > '+str(s.get('threshold',0)),'for':s['hold']} for s in specs if s['uid'] in uids]
(out/'rules.json').write_text(json.dumps({'groups':[{'name':'production','rules':rules}]}))
tests=[]
for title,inputs,expected in [('healthy idle',[('yeodam_connect_expected_connector_running{instance="10.20.10.130:9187",job="v2-connect-presence"}','1x20'),('yeodam_connect_expected_tasks_running{instance="10.20.10.130:9187"}','1x20'),('yeodam_observer_success{probe="connect"}','1x20'),('yeodam_observer_success{probe="mysql"}','1x20'),('yeodam_outbox_oldest_pending_seconds','0x20')],[]),('connector missing',[('yeodam_connect_expected_connector_running{instance="10.20.10.130:9187"}','0x20')],['v2p-connector-missing']),('connector running but task stopped',[('yeodam_connect_expected_connector_running{instance="10.20.10.130:9187"}','1x20'),('yeodam_connect_expected_tasks_running{instance="10.20.10.130:9187"}','0x20')],['v2p-connector-missing']),('absent connector series',[],['v2p-connector-missing']),('pending old event',[('yeodam_outbox_oldest_pending_seconds','300x20')],['v2p-outbox-stuck']),('probe unavailable',[('yeodam_observer_success{probe="mysql"}','0x20')],['v2p-observer-failed'])]:
 tested=expected or list(uids)
 tests.append({'name':title,'interval':'30s','input_series':[{'series':x,'values':v} for x,v in inputs],'alert_rule_test':[{'eval_time':'5m','alertname':uid,'exp_alerts':[{'exp_labels':{},'exp_annotations':{}}] if uid in expected else []} for uid in tested]})
for running in (True,False):
 inputs=[{'series':'yeodam_host_log_ship_timestamp_seconds{service="mysql"}','values':'0+30x30'},{'series':'yeodam_host_log_ship_timestamp_seconds{service="ai-gpu"}','values':'0x30'}]
 if running:inputs.append({'series':'up{job="v2-ai-gpu-agent",ec2_state="running"}','values':'1x30'})
 tests.append({'name':'GPU log stale running='+str(running),'interval':'30s','input_series':inputs,'alert_rule_test':[{'eval_time':'10m','alertname':'v2p-host-logs-stale','exp_alerts':[{'exp_labels':{},'exp_annotations':{}}] if running else []}]})
(out/'tests.json').write_text(json.dumps({'rule_files':['rules.json'],'evaluation_interval':'30s','tests':tests}))
