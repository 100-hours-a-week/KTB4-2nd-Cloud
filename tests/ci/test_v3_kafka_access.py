"""Boundary regressions: business names, role ownership, bounded storage and disposable tests."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('access', ROOT / 'scripts/check-v3-kafka-access.py')
a = importlib.util.module_from_spec(spec);spec.loader.exec_module(a)


class KafkaAccess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = a.kafka.platform.load_documents(a.render([a.FOUNDATION]))
        cls.probes = a.kafka.platform.load_documents(a.render([a.FOUNDATION, ROOT / 'tests/ci/v3/kafka-access-check.values.yaml']))

    def test_defaults_do_not_reserve_resources(self):
        a.validate(a.kafka.platform.load_documents(a.render()))
        self.assertEqual(len(self.docs), 24)
        self.assertEqual(len(self.probes), 30)

    def test_business_crs_and_optional_job_match_pinned_schemas(self):
        for docs, verify in [(self.docs, False), (self.probes, True)]:
            a.validate(docs, True, verify)
            a.kafka.platform.schema_check(yaml.safe_dump_all(docs))

    def test_replica_retention_timestamp_size_and_partition_mutations_rejected(self):
        for mutation in ('replicas', 'isr', 'retention', 'size', 'timestamp', 'partition', 'cleanup'):
            docs=deepcopy(self.docs);topic=next(d for d in docs if d['metadata']['name']=='prod.yeodam.image.initial-light')
            if mutation=='replicas':topic['spec']['replicas']=1
            if mutation=='isr':topic['spec']['config']['min.insync.replicas']=1
            if mutation=='retention':topic['spec']['config']['retention.bytes']=-1
            if mutation=='size':topic['spec']['config']['max.message.bytes']=1048576
            if mutation=='timestamp':topic['spec']['config']['message.timestamp.type']='CreateTime'
            if mutation=='partition':topic['spec']['partitions']=3
            if mutation=='cleanup':topic['spec']['config']['cleanup.policy']='compact'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):a.validate(docs,True)

    def test_no_wildcard_admin_other_group_or_cross_role_permission(self):
        for mutation in ('wildcard', 'group-prefix', 'admin', 'read-other', 'api-write'):
            docs=deepcopy(self.docs);user=next(d for d in docs if d['metadata']['name']=='v3-photo-converter')
            rules=user['spec']['authorization']['acls']
            if mutation=='wildcard':rules[0]['resource']['name']='*'
            if mutation=='group-prefix':rules[-1]['resource']['patternType']='prefix'
            if mutation=='admin':rules[0]['operations'].append('Alter')
            if mutation=='read-other':rules[0]['resource']['name']='prod.yeodam.story-create.result'
            if mutation=='api-write':next(d for d in docs if d['metadata']['name']=='v3-auth-api')['spec']['authorization']['acls'][0]['operations'].append('Write')
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):a.validate(docs,True)

    def test_namespace_cluster_duplicate_deletion_and_extra_resource_rejected(self):
        for mutation in ('namespace','cluster','duplicate','keep','extra','literal-password'):
            docs=deepcopy(self.docs)
            if mutation=='namespace':docs[0]['metadata']['namespace']='production'
            if mutation=='cluster':docs[0]['metadata']['labels']['strimzi.io/cluster']='production'
            if mutation=='duplicate':docs[-1]=deepcopy(docs[-2])
            if mutation=='keep':docs[0]['metadata'].pop('annotations')
            if mutation=='extra':docs.append({'kind':'Secret','metadata':{'name':'unexpected'}})
            if mutation=='literal-password':next(d for d in docs if d['kind']=='KafkaUser')['spec']['authentication']['password']='secret'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):a.validate(docs,True)

    def test_be_and_ai_use_handoff_names_and_existing_group_conventions(self):
        env=a.CONTRACT['backendEnvironment']
        self.assertEqual(env['KAFKA_TRIP_CREATE_REQUEST_TOPIC'],'prod.yeodam.trip-create.request')
        self.assertEqual(a.CONTRACT['users']['ai']['groups'],['yeodam-ai-trip-create','yeodam-ai-trip-update','yeodam-ai-story-create'])
        self.assertEqual(len(a.CONTRACT['users']['photo-converter']['groups']),5)
        self.assertEqual(len(a.CONTRACT['users']['trip-result-worker']['groups']),4)
        self.assertFalse(any('receipt' in t['name'] for t in a.CONTRACT['topics']))
        self.assertEqual(a.CONTRACT['users']['connect-outbox']['readTopics'],[])
        self.assertEqual(len(a.CONTRACT['users']['connect-outbox']['writeTopics']),7)

    def test_disposable_verification_cannot_gain_permissions_or_reserve_app_node(self):
        for mutation in ('identity','node','token','budget','script','persistent','secret','parallelism','env-secret'):
            docs=deepcopy(self.probes);job=next(d for d in docs if d['kind']=='Job');pod=job['spec']['template']['spec'];c=pod['containers'][0]
            if mutation=='identity':next(d for d in docs if d['metadata']['name']=='v3-access-writer')['spec']['authorization']['acls'][0]['resource']['name']='prod.yeodam.image.initial-light'
            if mutation=='node':pod['nodeSelector']={'yeodam.io/role':'api'}
            if mutation=='token':pod['automountServiceAccountToken']=True
            if mutation=='budget':c['resources']['limits']['memory']='1Gi'
            if mutation=='script':c['args']=['echo bypass']
            if mutation=='persistent':next(d for d in docs if d['metadata']['name']=='v3-access-other')['metadata']['annotations']={'helm.sh/resource-policy':'keep'}
            if mutation=='secret':pod['volumes'][1]['secret']['secretName']='production-credentials'
            if mutation=='parallelism':job['spec']['parallelism']=5
            if mutation=='env-secret':c['envFrom']=[{'secretRef':{'name':'other'}}]
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):a.validate(docs,True,True)

    def test_values_cannot_expand_storage_or_target_another_cluster(self):
        for data in ({'clusterName':'production'}, {'retentionBytes':-1}, {'replicas':1}, {'unexpected':'override'}):
            with tempfile.TemporaryDirectory() as folder:
                p=Path(folder)/'bad.yaml';p.write_text(yaml.safe_dump({'enabled':True,**data}))
                with self.assertRaises(subprocess.CalledProcessError):a.render([p])

    def test_verification_requires_opt_in(self):
        with self.assertRaises(ValueError):a.validate(self.probes,True,False)
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'bad.yaml';p.write_text('verification: {enabled: true}\n')
            with self.assertRaises(subprocess.CalledProcessError):a.render([p])

    def test_console_consumer_zero_exit_rejection_is_not_mistaken_for_success(self):
        script=(a.CHART/'files/verify.sh').read_text()
        function=script[script.index('expect_denied() {'):script.index('# Console producer')]
        for message, code, wanted in [('GroupAuthorizationException',0,0),
                ('TopicAuthorizationException',1,0),('Network timeout',1,1),('normal response',0,1)]:
            with tempfile.TemporaryDirectory() as folder:
                # Use a private log path; never touch the running Job's fixed location.
                fn=function.replace('/tmp/denial.log',str(Path(folder)/'denial.log'))
                source=fn+f"\nfake() {{ printf '%s\\n' '{message}'; return {code}; }}\n"+"expect_denied 'TopicAuthorizationException|GroupAuthorizationException' fake"
                result=subprocess.run(['bash','-c',source],capture_output=True,text=True)
                with self.subTest(message=message,code=code):self.assertEqual(result.returncode,wanted)

    def test_reviewed_shell_checks_authorization_failures_not_just_exit_codes(self):
        script=(a.CHART/'files/verify.sh').read_text()
        subprocess.run(['bash','-n'],input=script,text=True,check=True)
        self.assertIn('ssl.endpoint.identification.algorithm=https',script)
        for marker in ('TopicAuthorizationException','GroupAuthorizationException','SaslAuthenticationException'):
            self.assertIn(marker,script)
        self.assertNotIn('prod.yeodam.',script)
        self.assertNotIn('set -x',script)


if __name__ == '__main__':unittest.main()
