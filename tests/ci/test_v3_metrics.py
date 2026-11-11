import copy
import importlib.util
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
chart=load('metrics_chart','scripts/check-v3-metrics.py')
cdc=load('cdc_metrics','deploy/v3/metrics/files/cdc-status.py')
def status(task='RUNNING'):
    return {'name':cdc.CONNECTOR,'connector':{'state':'RUNNING'},'tasks':[{'id':0,'state':task,'trace':'private detail must not be exported'}]}
class CDC(unittest.TestCase):
    def test_connector_running_does_not_hide_task_failed(self):
        out=cdc.metrics(lambda:status('FAILED'))
        self.assertIn('yeodam_cdc_connector_state{connector="yeodam-v3-outbox",state="RUNNING"} 1',out)
        self.assertIn('task="0",state="FAILED"} 1',out)
        self.assertNotIn('private detail',out)
    def test_failed_observation_is_unknown_not_fake_healthy(self):
        def fail():raise OSError('sensitive fixture')
        out=cdc.metrics(fail)
        self.assertIn('yeodam_cdc_probe_success 0',out)
        self.assertNotIn('state=',out);self.assertNotIn('sensitive',out)
    def test_missing_task_not_reported_running(self):
        s=status();s['tasks']=[];out=cdc.metrics(lambda:s)
        self.assertIn('yeodam_cdc_expected_task_present 0',out)
        self.assertNotIn('task="0",state="RUNNING"} 1',out)
    def test_unknown_state_and_wrong_connector_rejected(self):
        for s in [status('NEW_STATE'),dict(status(),name='unexpected')]:
            self.assertIn('yeodam_cdc_probe_success 0',cdc.metrics(lambda:s))
class Chart(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.docs=chart.render()
    def test_disabled_and_auth_optional_without_plaintext(self):
        self.assertEqual(chart.render(False),[]);self.assertTrue(chart.validate(self.docs));self.assertTrue(chart.validate(chart.render(ai=False)))
    def test_public_or_write_authority_rejected(self):
        for kind,change in [('Service',lambda d:d['spec'].update(type='LoadBalancer')),('ClusterRole',lambda d:d['rules'][0]['verbs'].append('create'))]:
            docs=copy.deepcopy(self.docs);change(next(x for x in docs if x['kind']==kind))
            with self.assertRaises(ValueError):chart.validate(docs)
    def test_replica_increase_or_new_node_placement_rejected(self):
        for mutate in [lambda d:d['spec'].update(replicas=2),lambda d:d['spec']['template']['spec'].update(nodeSelector={})]:
            docs=copy.deepcopy(self.docs);mutate(next(x for x in docs if x['kind']=='Deployment'))
            with self.assertRaises(ValueError):chart.validate(docs)
    def test_self_scrape_growth_requires_filter_instead_of_larger_budget(self):
        import yaml
        for mutate in [lambda x:x.pop('metric_relabel_configs'),lambda x:x.update(sample_limit=2000),lambda x:x['metric_relabel_configs'][0].update(regex='.*')]:
            docs=copy.deepcopy(self.docs)
            data=next(x for x in docs if x['kind']=='ConfigMap' and x['metadata']['name']=='v3-metrics-config')['data']
            config=yaml.safe_load(data['prometheus.yaml']);own=next(x for x in config['scrape_configs'] if x['job_name']=='prometheus');mutate(own);data['prometheus.yaml']=yaml.safe_dump(config)
            with self.assertRaises(ValueError):chart.validate(docs)
    def test_apm_viewer_can_explore_without_editor_or_admin_role(self):
        docs=chart.render(apm=True);self.assertTrue(chart.validate(docs))
        for missing in [False,True]:
            bad=copy.deepcopy(docs);env=next(x for x in bad if x['kind']=='Deployment' and x['metadata']['name']=='v3-grafana')['spec']['template']['spec']['containers'][0]['env']
            if missing:env[:]=[x for x in env if x['name']!='GF_USERS_VIEWERS_CAN_EDIT']
            else:next(x for x in env if x['name']=='GF_AUTH_ANONYMOUS_ORG_ROLE')['value']='Editor'
            with self.assertRaises(ValueError):chart.validate(bad)
if __name__=='__main__'  :unittest.main()
