import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('experiment', ROOT/'scripts/collect-v3-experiment.py')
e = importlib.util.module_from_spec(spec); spec.loader.exec_module(e)


def series(value, **labels):
    return {'status': 'ok', 'series': [{'labels': labels, 'values': [[600, value]]}]}


class Evidence(unittest.TestCase):
    def test_zero_unknown_low_sample_and_nonfinite_are_distinct(self):
        for count, value, status in [(0, None, 'no_traffic'), (None, .5, 'unknown'),
                                      (3, .5, 'low_sample'), (30, None, 'histogram_unavailable'), (30, .5, 'threshold_met')]:
            data={k: series(count, **labels) for k, labels in [('be_count_estimate',{'workload':'trip-api'}),('ai_count_estimate',{'operation':'process','phase':'total'})]}
            data.update(be_p95_seconds=series(value, workload='trip-api'), ai_p95_seconds=series(value, operation='process',phase='total'))
            r=e.annotate_samples(data, min_samples=20)
            for kind in ['be','ai']:
                self.assertEqual(r[kind+'_p95_seconds']['series'][0]['sampleStatus'],status)
                if count is None or count==0:self.assertIsNone(r[kind+'_p95_seconds']['series'][0]['values'][0][1])

    def test_unspecified_sample_goal_is_not_invented(self):
        data={'be_count_estimate':series(100,workload='trip-api'),'be_p95_seconds':series(.5,workload='trip-api'),'ai_count_estimate':series(100,operation='process',phase='total'),'ai_p95_seconds':series(.5,operation='process',phase='total')}
        self.assertEqual(e.annotate_samples(data)['be_p95_seconds']['series'][0]['sampleStatus'],'adequacy_unassessed')

    def test_only_loopback_without_keys_or_redirects(self):
        for url in ['https://example.com','http://localhost:9090','http://169.254.169.254:80','http://token@127.0.0.1:9','http://127.0.0.1:9/path','http://127.0.0.1:9?token=secret']:
            with self.assertRaises(ValueError):e.Prometheus(url)
        e.Prometheus('http://127.0.0.1:9090')

    def test_real_http_missing_warning_nan_error_and_redirection(self):
        body={'status':'success','data':{'resultType':'matrix','result':[]}}
        code=200; location=None
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(code)
                if location:self.send_header('Location',location)
                self.end_headers();self.wfile.write(json.dumps(body).encode())
            def log_message(self,*a):pass
        server=HTTPServer(('127.0.0.1',0),Handler);t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        try:
            client=e.Prometheus(f'http://127.0.0.1:{server.server_port}')
            self.assertEqual(client.query('up',0,600,30)['status'],'missing')
            body['data']['result']=[{'metric':{'job':'backend','email':'private@example.invalid'},'values':[[30,'NaN'],[60,'0'],[90,'+Inf']]}]
            r=client.query('up',0,600,30);self.assertEqual(r['series'][0]['values'],[[30,None],[60,0],[90,None]])
            self.assertEqual(r['series'][0]['labels'],{'job':'backend'});self.assertEqual(r['nonFiniteSamples'],2)
            body['warnings']=['private-query-warning'];self.assertEqual(client.query('up',0,600,30)['status'],'partial')
            self.assertNotIn('private-query-warning',json.dumps(client.query('up',0,600,30)))
            code=500;self.assertEqual(client.query('up',0,600,30)['status'],'query_failed')
            code=302;location='http://169.254.169.254/';self.assertEqual(client.query('up',0,600,30)['status'],'query_failed')
        finally:server.shutdown();server.server_close();t.join()

    def test_whole_window_histogram_not_average_of_p95s(self):
        queries=[]
        class Client:
            def query(self,expr,start,end):queries.append(expr);return {'status':'missing','series':[]}
        e.counts_and_percentiles(Client(),0,600)
        self.assertTrue(all('[600s]' in q for q in queries))
        self.assertTrue(all('avg(' not in q for q in queries))
        self.assertEqual(e.RANGE['ai_jobs'],'max by (state) (yeodam_ai_jobs{job="ai"})')
        self.assertIn('uri!~"/actuator.*"',queries[1])

    def test_inventory_never_exports_env_or_auth_but_detects_config_change(self):
        docs=[{'metadata':{'name':name,'namespace':ns},'spec':{'replicas':1,'template':{'spec':{'containers':[{'name':'app','image':'fixture@sha256:123','resources':{'requests':{'memory':'32Mi'}},'env':[{'name':'API_KEY','value':'SYNTHETIC_SECRET'}]}]}}}} for name,ns in e.APPS.items()]
        def kube(config,*args):
            if 'deployments' in args:return {'items':docs}
            if 'nodes' in args:return {'items':[{'metadata':{'labels':{'node.kubernetes.io/instance-type':'t3a.small'}}}]}
            return {'status':{'sync':{'revision':'a'*40}}}
        with patch.object(e,'kube_json',kube):
            a=e.snapshot('fixture');self.assertNotIn('SYNTHETIC_SECRET',json.dumps(a))
            docs[0]['spec']['template']['spec']['containers'][0]['env'][0]['value']='DIFFERENT'
            b=e.snapshot('fixture');self.assertNotEqual(a['apps'],b['apps'])
            docs.pop()
            with self.assertRaises(ValueError):e.snapshot('fixture')

    def test_event_times_do_not_invent_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'events.json';p.write_text(json.dumps([{'event':'fault_injected','at':'2026-11-11T00:01:00Z'}]))
            start=e.timestamp('2026-11-11T00:00:00Z');self.assertEqual(len(e.validate_events(p,start,start+600)),1)
            for event in [[{'event':'recovered','at':'2026-11-11T00:01:00Z'}],[{'event':'fault_injected','at':'2026-11-10T00:00:00Z'}]]:
                p.write_text(json.dumps(event))
                with self.assertRaises(ValueError):e.validate_events(p,start,start+600)
        with self.assertRaises(ValueError):e.timestamp('2026-11-11T00:00:00')

    def test_private_creation_only_and_partial_report_end_to_end(self):
        before={'capturedAt':'2026-11-11T00:00:00Z','apps':[],'nodeTypes':{},'approvedRevision':'a'}
        after=copy.deepcopy(before);after['approvedRevision']='b'
        class Client:
            def __init__(self,*args):pass
            def query(self,*args):return {'status':'query_failed','errorType':'TimeoutError','series':[]}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);conditions=root/'conditions.json';e.save(conditions,before)
            self.assertEqual(conditions.stat().st_mode & 0o777,0o600)
            with self.assertRaises(FileExistsError):e.save(conditions,'overwrite')
            args=SimpleNamespace(start='2026-11-11T00:00:00Z',end='2026-11-11T00:10:00Z',conditions=conditions,kubeconfig='fixture',prometheus='http://127.0.0.1:9090',output=root/'evidence',events=None,experiment_id='synthetic',min_samples=None)
            with patch.object(e,'snapshot',return_value=after),patch.object(e,'Prometheus',Client):
                self.assertEqual(e.collect(args),2)
            result=json.loads((args.output/'result.json').read_text())
            self.assertEqual(result['collectionStatus'],'partial');self.assertFalse(result['conditionsMatch'])
            self.assertEqual(result['verdict'],'not_assessed');self.assertEqual(args.output.stat().st_mode & 0o777,0o700)
            self.assertIn('미수행', (args.output/'summary.md').read_text())
            with self.assertRaises(FileExistsError):
                with patch.object(e,'snapshot',return_value=after):e.collect(args)

if __name__=='__main__':unittest.main()
