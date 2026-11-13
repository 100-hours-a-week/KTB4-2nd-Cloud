import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import yaml
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
import v3_autoscaling as policy
def load(name,path):
    s=importlib.util.spec_from_file_location(name,ROOT/path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
chart=load('scaling_chart','scripts/check-v3-autoscaling.py')
apps=load('apps_chart','scripts/check-v3-apps.py')
argo=load('argo_chart','scripts/check-v3-argocd.py')
kafka=load('kafka_chart','scripts/check-v3-kafka-access.py')
outbox=load('outbox_metrics','deploy/v3/metrics/files/outbox-metrics.py')
provision=load('outbox_provision','scripts/provision-v3-outbox-observer.py')
class Scaling(unittest.TestCase):
    def test_disabled_is_empty(self):self.assertEqual(chart.render(),[])
    def test_missing_review_blocks_activation(self):
        for api,converter in [(True,False),(False,True)]:
            with self.assertRaises(subprocess.CalledProcessError):chart.render(True,api,converter)
    def test_api_cpu_target_and_maximum(self):
        docs=chart.render(True,True,False,True)
        self.assertEqual({d['metadata']['name']for d in docs},set(policy.API))
        for field,value in [('maxReplicas',3),('minReplicas',0)]:
            bad=copy.deepcopy(docs);bad[0]['spec'][field]=value
            with self.assertRaises(ValueError):chart.validate(bad,api=True)
    def test_kafka_one_partition_cannot_activate(self):
        with self.assertRaises(subprocess.CalledProcessError):chart.render(True,False,True,False)
    def test_four_topics_and_no_consume_privilege(self):
        docs=chart.render(True,False,True,True)
        users=[d for d in docs if d['kind']=='KafkaUser'];self.assertEqual(len(users),1)
        self.assertTrue(all(x['operations']==['Describe']for x in users[0]['spec']['authorization']['acls']))
        scale=next(d for d in docs if d['kind']=='ScaledObject')
        self.assertEqual({x['metadata']['consumerGroup']for x in scale['spec']['triggers']},{'prod.yeodam.image-conversion-'+x for x in policy.LANES})
        self.assertEqual(scale['spec']['fallback']['behavior'],'currentReplicasIfHigher')
    def test_candidate_only_relinquishes_selected_ownership(self):
        c=json.loads((ROOT/'deploy/v3/releases/candidate.json').read_text())
        for profile in ('frontend','backend'):c['values'][profile].pop('autoscaledWorkloads',None)
        n=policy.prepare_candidate(c)
        for profile in ('frontend','backend'):n['values'][profile].pop('autoscaledWorkloads')
        self.assertEqual(c,n)
    def test_snapshot_preserves_templates_except_converter_placement(self):
        c=json.loads((ROOT/'deploy/v3/releases/candidate.json').read_text())
        for profile in ('frontend','backend'):c['values'][profile].pop('autoscaledWorkloads',None)
        n=policy.prepare_candidate(c,True)
        for profile in ('frontend','backend'):
            with tempfile.TemporaryDirectory() as tmp:
                docs=[]
                for state in (c,n):
                    path=Path(tmp)/'values.yaml';path.write_text(yaml.safe_dump(state['values'][profile]))
                    v=apps.load_values([path]);d=apps.platform.load_documents(apps.render([path]));apps.validate(d,v);docs.append(d)
                before={(d['kind'],d['metadata']['name']):d for d in docs[0]}
                for d in docs[1]:
                    old=before[d['kind'],d['metadata']['name']]
                    if d['kind']=='Deployment'and d['metadata']['name']in n['values'][profile]['autoscaledWorkloads']:
                        self.assertNotIn('replicas',d['spec']);old=copy.deepcopy(old);old['spec'].pop('replicas')
                        if d['metadata']['name']=='photo-converter':
                            affinity={'podAntiAffinity':{'requiredDuringSchedulingIgnoredDuringExecution':[{'labelSelector':{'matchLabels':{'app.kubernetes.io/instance':'photo-converter'}},'topologyKey':'kubernetes.io/hostname'}]}}
                            self.assertEqual(d['spec']['template']['spec']['affinity'],affinity)
                            old['spec']['template']['spec']['affinity']=affinity
                    self.assertEqual(old,d)
    def test_worker_or_ai_ownership_widening_rejected(self):
        c=json.loads((ROOT/'deploy/v3/releases/candidate.json').read_text())['values']['backend'];c['autoscaledWorkloads']=['trip-result-worker']
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'bad.yaml';p.write_text(yaml.safe_dump(c))
            with self.assertRaises(subprocess.CalledProcessError):apps.render([p])
    def test_argo_ignores_only_five_replica_fields(self):
        docs=list(yaml.safe_load_all(argo.render_access(enabled=True,appsAdoptionReviewed=True,automatedSync=True,autoscalingAdoptionReviewed=True)))
        argo.validate_access(docs,True,True,True)
        app=next(x for x in docs if x['kind']=='Application')
        self.assertEqual(len(app['spec']['ignoreDifferences']),5)
        app['spec']['ignoreDifferences'][0]['jsonPointers'].append('/spec/template')
        with self.assertRaises(ValueError):argo.validate_access(docs,True,True,True)
    def test_expansion_requires_review_and_changes_only_four_partitions(self):
        args=[os.environ['V3_HELM'],'template','access',str(ROOT/'deploy/v3/kafka-access'),'--set','enabled=true,imagePartitions=2']
        with self.assertRaises(subprocess.CalledProcessError):subprocess.check_output(args,stderr=subprocess.DEVNULL)
        expanded=list(yaml.safe_load_all(subprocess.check_output(args+['--set','imagePartitionExpansionReviewed=true'],text=True)))
        kafka.validate(expanded,enabled=True,image_partitions=2)
        initial=list(yaml.safe_load_all(subprocess.check_output([os.environ['V3_HELM'],'template','access',str(ROOT/'deploy/v3/kafka-access'),'--set','enabled=true'],text=True)))
        for old,new in zip(initial,expanded,strict=True):
            if old['kind']=='KafkaTopic'and old['metadata']['name']in {'prod.yeodam.image.'+l for l in policy.LANES}:old['spec']['partitions']=2
            self.assertEqual(old,new)
    def test_missing_metrics_api_fails_preflight(self):
        with self.assertRaises(ValueError):policy.validate_prerequisites([])
class Capacity(unittest.TestCase):
    def test_capacity_counts_other_namespaces_and_init_requests(self):
        m=load('inspection','scripts/inspect-v3-autoscaling.py')
        node={'metadata':{'name':'cpu','uid':'n','labels':{'yeodam.io/role':'worker','yeodam.io/stage':'v3'}},'spec':{},'status':{'allocatable':{'pods':'17','cpu':'2','memory':'4Gi'},'conditions':[{'type':'Ready','status':'True'}]}}
        pods=[{'metadata':{'namespace':'other'},'spec':{'nodeName':'cpu','containers':[{'resources':{'requests':{'cpu':'100m','memory':'1Gi'}}}],'initContainers':[{'resources':{'requests':{'cpu':'1900m','memory':'1Gi'}}}]},'status':{'phase':'Running'}}]
        self.assertEqual(m.capacity([node],pods)[0]['managementPodSlots'],4)
        node['metadata']['labels']['yeodam.io/role']='gpu';self.assertEqual(m.capacity([node],pods),[])
    def test_existing_namespaced_keda_hpa_is_not_conflicting(self):
        objects=[{'kind':'APIService','metadata':{'name':'v1beta1.metrics.k8s.io'},'status':{'conditions':[{'type':'Available','status':'True'}]}}]
        for name,ns in policy.API.items():objects.append({'kind':'Deployment','metadata':{'name':name,'namespace':ns},'status':{'availableReplicas':1}})
        self.assertEqual(policy.validate_prerequisites(objects)['maxReplicas'],2)
        objects.append({'kind':'HorizontalPodAutoscaler','metadata':{'namespace':'auth','name':'unexpected'},'spec':{'scaleTargetRef':{'name':'auth-api'}}})
        with self.assertRaises(ValueError):policy.validate_prerequisites(objects)

class Outbox(unittest.TestCase):
    def sample(self,rows=None,stamp=100):
        rows = rows or [t+'\t2\t60.5'for t in outbox.TABLES]
        count = rows[-1].split('\t')[1]
        return str(stamp)+'\n'+ '\n'.join(rows + ['conversion_'+c+'\t'+(count+'\t60.5' if c=='active' else '0\t0') for c in outbox.CLASSES])+'\n'
    def parse(self,text,now=110):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'snapshot';path.write_text(text);return outbox.metrics(path,now)
    def test_exact_four_aggregates(self):
        s=self.parse(self.sample());self.assertIn('yeodam_outbox_probe_success 1',s);self.assertEqual(s.count('yeodam_outbox_pending{'),4)
    def test_stale_future_missing_and_truncated_are_unknown(self):
        for text in [self.sample(stamp=1),self.sample(stamp=120),self.sample().rsplit('\n',2)[0],self.sample().replace('60.5','NaN'),self.sample().replace('60.5','-1')]:
            self.assertEqual(self.parse(text),'yeodam_outbox_probe_success 0\n')
        self.assertEqual(outbox.metrics('/does-not-exist'),'yeodam_outbox_probe_success 0\n')
    def test_duplicate_or_unreviewed_label_is_unknown(self):
        for rows in [[outbox.TABLES[0]+'\t1\t1']*4, ['user-provided-id\t1\t1']*4]:self.assertEqual(self.parse(self.sample(rows)),'yeodam_outbox_probe_success 0\n')
    def test_capped_count_is_labelled_lower_bound(self):
        s=self.parse(self.sample([t+'\t10001\t100'for t in outbox.TABLES]));self.assertIn('yeodam_outbox_count_capped{outbox="trip_creation_outbox"} 1',s)
    def test_classification_is_complete_or_entire_sample_unknown(self):
        valid=self.sample()
        self.assertIn('yeodam_conversion_outbox_pending{category="active"} 2',self.parse(valid))
        for invalid in [valid.replace('conversion_active\t2', 'conversion_active\t1'), valid.replace('conversion_unknown\t0\t0\n',''), valid.replace('conversion_unknown','conversion_new-state')]:
            self.assertEqual(self.parse(invalid),'yeodam_outbox_probe_success 0\n')
        capped=self.parse(self.sample([t+'\t10001\t100'for t in outbox.TABLES]))
        self.assertIn('yeodam_conversion_outbox_classification_capped 1',capped)
    def test_readonly_sql_and_privileges(self):
        source=(ROOT/'deploy/v3/metrics/files/outbox.sql').read_text()
        self.assertEqual(source.count('MAX_EXECUTION_TIME(750)'),5);self.assertEqual(source.count('LIMIT 10001'),5);self.assertIn('TRANSACTION READ ONLY',source)
        self.assertNotIn('payload',source);self.assertNotIn('trip_id',source)
        grants=provision.sql('a'*48);self.assertEqual(grants.count('GRANT SELECT (status,cdc_managed,created_at)'),4);self.assertNotIn('ON *.*',grants)
        self.assertIn("GRANT SELECT (task_id,status) ON yeodam.attachment_conversion_tasks",grants)
        self.assertIn("GRANT SELECT (task_id) ON yeodam.attachment_conversion_outbox",grants)
        with self.assertRaises(ValueError):provision.sql("'injection")
    def test_sidecar_is_opt_in_and_retains_all_existing_container_budgets(self):
        args=[os.environ['V3_HELM'],'template','metrics',str(ROOT/'deploy/v3/metrics'),'--set','enabled=true,outboxEnabled=true,autoscalingMetricsEnabled=true']
        docs=[x for x in yaml.safe_load_all(subprocess.check_output(args,text=True))if x]
        self.assertEqual(sum(x['kind']=='Deployment'for x in docs),4);self.assertEqual(sum(x['kind']=='PersistentVolumeClaim'for x in docs),1)
        dep=next(x for x in docs if x['kind']=='Deployment'and x['metadata']['name']=='v3-cdc-status')
        self.assertEqual(len(dep['spec']['template']['spec']['containers']),2)
        cfg=next(x for x in docs if x['kind']=='ConfigMap'and x['metadata']['name']=='v3-metrics-config')['data']
        rules=yaml.safe_load(cfg['alerts.yaml']);self.assertEqual(len(rules['groups']),2)
        prom=yaml.safe_load(cfg['prometheus.yaml']);self.assertIn('keda',[x['job_name']for x in prom['scrape_configs']])
        chart.platform.schema_check(yaml.safe_dump_all(docs))
if __name__=='__main__':unittest.main()
