import copy,importlib.util,json,os,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
def module(name,file):
    s=importlib.util.spec_from_file_location(name,ROOT/'scripts'/file);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
ai=module('ai_contract','check-v3-ai.py');source=module('ai_source','inspect-v3-ai-source.py');prepare=module('ai_prepare','prepare-v3-ai-values.py');guard=module('ai_plan','check-v3-ai-plan.py')
def compatible_files():
    files={k:'' for k in source.FILES}
    files['app/jobs/config.py']='class JobsSettings:\n KAFKA_SECURITY_PROTOCOL: Literal["PLAINTEXT", "SASL_SSL"]\n'+''.join(' '+k+': str\n' for k in ['KAFKA_SASL_MECHANISM','KAFKA_SASL_USERNAME','KAFKA_SASL_PASSWORD','KAFKA_SSL_CAFILE','PLACE_INDEX_PATH','PLACE_INDEX_SHA256','PLACE_INDEX_S3_URI','KAKAO_REST_API_KEY'])
    files['app/core/config.py']='class Settings:\n QDRANT_API_KEY: str\n'
    files['app/jobs/kafka.py']='def connection_options(cfg):\n return {"ssl_context": cfg.context, "sasl_mechanism":cfg.mechanism,"sasl_plain_username":cfg.username,"sasl_plain_password":cfg.password}\n'
    files['app/infra/qdrant.py']='a=QdrantClient(url=location,api_key=key)\nb=AsyncQdrantClient(url=location,api_key=key)\n'
    files['app/jobs/runner.py']='prepare_place_index(cfg)\n'
    return files
class AITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.values=ai.fixture();cls.docs=ai.platform.load_documents(ai.render(cls.values))
    def mutate(self,callback):
        docs=copy.deepcopy(self.docs);callback(docs)
        with self.assertRaises((ValueError,KeyError,StopIteration)):ai.validate(docs,self.values)
    def deployment(self,ds):return next(d for d in ds if d['kind']=='Deployment')
    def test_default_empty(self):self.assertEqual(ai.platform.load_documents(ai.render()),[])
    def test_full_contract(self):ai.validate(self.docs,self.values)
    def test_unreviewed_source_blocks(self):
        v=copy.deepcopy(self.values);v['review']['sourceCompatible']=False
        with self.assertRaises(subprocess.CalledProcessError):ai.render(v)
    def test_unreviewed_models_block(self):
        v=copy.deepcopy(self.values);v['review']['connectionsReviewed']=False
        with self.assertRaises(subprocess.CalledProcessError):ai.render(v)
    def test_mutable_image_blocks(self):
        v=copy.deepcopy(self.values);v['image']='ghcr.io/100-hours-a-week/yeodam-ai-app:latest'
        with self.assertRaises(subprocess.CalledProcessError):ai.render(v)
    def test_replica_increase(self):self.mutate(lambda ds:self.deployment(ds)['spec'].update(replicas=2))
    def test_resource_increase(self):self.mutate(lambda ds:self.deployment(ds)['spec']['template']['spec']['containers'][0]['resources']['limits'].update(memory='16Gi'))
    def test_wrong_nodepool(self):self.mutate(lambda ds:self.deployment(ds)['spec']['template']['spec'].update(nodeSelector={'yeodam.io/role':'data'}))
    def test_secret_payload(self):self.mutate(lambda ds:ds.append({'kind':'Secret','metadata':{'name':'bad','namespace':'ai'}}))
    def test_cross_source(self):self.mutate(lambda ds:next(d for d in ds if d['kind']=='ExternalSecret')['spec']['data'][0]['remoteRef'].update(key='yeodam/v2/production/ai'))
    def test_public_service(self):self.mutate(lambda ds:next(d for d in ds if d['kind']=='Service')['spec'].update(type='LoadBalancer'))
    def test_network_widening(self):self.mutate(lambda ds:next(d for d in ds if d['kind']=='NetworkPolicy')['spec'].update(egress=[{}]))
    def test_production_queue_prefix(self):self.mutate(lambda ds:next(d for d in ds if d['kind']=='ConfigMap' and d['metadata']['name']=='ai-api-runtime')['data'].update(JOBS_ENV='production'))
    def test_no_host_storage(self):self.mutate(lambda ds:self.deployment(ds)['spec']['template']['spec']['volumes'].append({'name':'host','hostPath':{'path':'/'}}))
    def test_source_screen_valid(self):self.assertEqual(source.inspect(compatible_files(),'b'*40)['status'],'compatible')
    def test_source_plaintext_blocks(self):
        f=compatible_files();f['app/jobs/config.py']=f['app/jobs/config.py'].replace(', "SASL_SSL"','');r=source.inspect(f,'b'*40);self.assertIn('kafkaProtocol',r['missing'])
    def test_qdrant_both_clients_required(self):
        f=compatible_files();f['app/infra/qdrant.py']=f['app/infra/qdrant.py'].replace('AsyncQdrantClient(url=location,api_key=key)','AsyncQdrantClient(url=location)');self.assertIn('qdrantClients',source.inspect(f,'b'*40)['missing'])
    def reviews(self):
        v=self.values;return source.inspect(compatible_files(),v['source']),{'image':v['image'],'source':v['source'],'runAsUser':10001,'registryDigestVerified':True,'readOnlyRootSupported':True,'minimumMemoryReviewed':True,'healthDependenciesAvailable':True},{'sharedCapacityApproved':True,'routeAndSecurityGroupVerified':True,'privatePeers':[]}
    def test_staged_inputs(self):
        screen,image,models=self.reviews();r=prepare.prepare(self.values['image'],self.values['source'],screen,image,models,self.values['trust'],'1'*32);self.assertFalse(r['access.values.json']['workloads']['api']);self.assertFalse(r['api.values.json']['workloads']['runner']);self.assertTrue(r['full.values.json']['workloads']['runner'])
    def test_source_mismatch_blocks(self):
        s,i,m=self.reviews();s['source']='d'*40
        with self.assertRaises(ValueError):prepare.prepare(self.values['image'],self.values['source'],s,i,m,self.values['trust'],'1'*32)
    def test_blocked_screen_blocks(self):
        s,i,m=self.reviews();s['status']='blocked'
        with self.assertRaises(ValueError):prepare.prepare(self.values['image'],self.values['source'],s,i,m,self.values['trust'],'1'*32)
    def test_private_model_cidr_widening_blocks(self):
        s,i,m=self.reviews();m['privatePeers']=[{'cidr':'10.30.0.0/16','port':8000}]
        with self.assertRaises(ValueError):prepare.prepare(self.values['image'],self.values['source'],s,i,m,self.values['trust'],'1'*32)
    def test_image_review_secret_field_blocks(self):
        s,i,m=self.reviews();i['password']='must-not-enter-public-inputs'
        with self.assertRaises(ValueError):prepare.prepare(self.values['image'],self.values['source'],s,i,m,self.values['trust'],'1'*32)
    def test_private_key_in_trust_blocks(self):
        s,i,m=self.reviews();trust=copy.deepcopy(self.values['trust']);trust['redis']='PRIVATE KEY'
        with self.assertRaises(ValueError):prepare.prepare(self.values['image'],self.values['source'],s,i,m,trust,'1'*32)
    def test_plan_noop(self):self.assertEqual(guard.validate({'complete':True}),{'creates':0,'updates':0,'deletes':0})
    def test_plan_gpu_change_blocks(self):
        with self.assertRaises(ValueError):guard.validate({'complete':True,'resource_changes':[{'mode':'managed','type':'aws_instance','address':'aws_instance.gpu','change':{'actions':['update']}}]})
    def test_api_liveness_dependency503(self):
        s=importlib.util.spec_from_file_location('ai_health',ROOT/'deploy/v3/ai-apps/files/runtime/health.py');h=importlib.util.module_from_spec(s);s.loader.exec_module(h)
        from urllib.error import HTTPError
        with patch.object(h.urllib.request,'urlopen',side_effect=HTTPError('http://local',503,'degraded',{},None)):self.assertTrue(h.api_live())
        with patch.object(h.urllib.request,'urlopen',side_effect=HTTPError('http://local',500,'bad',{},None)):self.assertFalse(h.api_live())
    def test_secret_version_changes_revision(self):
        screen,image,models=self.reviews();args=(self.values['image'],self.values['source'],screen,image,models,self.values['trust']);a=prepare.prepare(*args,'1'*32);b=prepare.prepare(*args,'2'*32)
        self.assertNotEqual(a['full.values.json']['configurationRevision'],b['full.values.json']['configurationRevision'])
        self.assertEqual(a['backend-ai.values.json']['defaults']['configMapData']['AI_SERVER_BASE_URL'],'http://ai-api.ai.svc.cluster.local:8000')
    def test_file_entrypoint_resolves_app_with_rendered_environment(self):
        # Reproduce Python file execution outside /app, using the actual rendered runner CM.
        docs = ai.platform.load_documents(ai.render(self.values))
        env = next(d['data'] for d in docs if d['kind'] == 'ConfigMap' and d['metadata']['name'] == 'ai-runner-runtime')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'app').mkdir()
            (root / 'app/__init__.py').write_text('')
            (root / 'runtime').mkdir()
            entry = root / 'runtime/probe.py'
            entry.write_text('import app; print("resolved")')
            clean = dict(os.environ);clean.pop('PYTHONPATH', None)
            self.assertNotEqual(subprocess.run([os.sys.executable, str(entry)], cwd=root, env=clean, capture_output=True).returncode, 0)
            self.assertEqual(env['PYTHONPATH'], '/app')
            clean['PYTHONPATH'] = str(root)  # temporary fixture represents the image /app directory
            result = subprocess.run([os.sys.executable, str(entry)], cwd=root, env=clean, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), 'resolved')

    def test_exact_private_model_peer(self):
        v=copy.deepcopy(self.values);v['privateModelPeers']=[{'cidr':'10.20.10.162/32','port':8000}];ai.validate(ai.platform.load_documents(ai.render(v)),v)
    def plan_fixture(self):
        changes=[]
        for role in ['ai-api','ai-runner']:
            condition={'StringEquals':{'aws:SourceAccount':'483175530259','aws:RequestTag/kubernetes-namespace':'ai','aws:RequestTag/kubernetes-service-account':role},'ArnEquals':{'aws:SourceArn':'arn:aws:eks:ap-northeast-2:483175530259:cluster/yeodam-v3-foundation'}}
            trust={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'pods.eks.amazonaws.com'},'Action':['sts:AssumeRole','sts:TagSession'],'Condition':condition}]}
            objects=['arn:aws:s3:::yeodam-v3-foundation-app-data-483175530259-ap-northeast-2/trip-uploads/*/derived/*','arn:aws:s3:::yeodam-v3-foundation-app-data-483175530259-ap-northeast-2/trip-additions/*/derived/*']
            if role=='ai-runner':objects+=['arn:aws:s3:::yeodam-v1-app-data-483175530259-ap-northeast-2/ai-reference/places/v2026-10-30/places.sqlite',guard.TOURAPI]
            records=[('aws_iam_role','app',{'name':'yeodam-v3-foundation-'+role,'assume_role_policy':json.dumps(trust)}),('aws_eks_pod_identity_association','app',{'cluster_name':'yeodam-v3-foundation','namespace':'ai','service_account':role}),('aws_iam_role_policy','read',{'policy':json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['s3:GetObject'],'Resource':objects}]})})]
            for typ,name,after in records:changes.append({'mode':'managed','type':typ,'address':typ+'.'+name+'["'+role+'"]','change':{'actions':['create'],'after':after}})
        return {'complete':True,'resource_changes':changes}
    def test_identity_plan_positive(self):self.assertEqual(guard.validate(self.plan_fixture()),{'creates':6,'updates':0,'deletes':0})
    def test_identity_write_permission_rejected(self):
        p=self.plan_fixture();record=p['resource_changes'][-1];policy=json.loads(record['change']['after']['policy']);policy['Statement'][0]['Action']+=['s3:PutObject'];record['change']['after']['policy']=json.dumps(policy)
        with self.assertRaises(ValueError):guard.validate(p)
    def test_revision_annotation_required(self):self.mutate(lambda ds:self.deployment(ds)['spec']['template']['metadata']['annotations'].pop('checksum/runtime'))
    def test_chart_config_change_replaces_pod(self):
        import shutil,yaml
        with tempfile.TemporaryDirectory() as d:
            chart=Path(d)/'chart';shutil.copytree(ai.CHART,chart,ignore=shutil.ignore_patterns('__pycache__','*.pyc'));p=chart/'files/contract.json';v=json.loads(p.read_text());v['commonEnvironment']['STORY_MAX_TOKENS']='1024';p.write_text(json.dumps(v))
            source=subprocess.check_output([os.environ['V3_HELM'],'template','check',str(chart),'-n','ai','--kube-version','1.36.0','-f','-'],input=yaml.safe_dump(self.values),text=True);new=ai.platform.load_documents(source)
            self.assertNotEqual(self.deployment(self.docs)['spec']['template']['metadata']['annotations']['checksum/runtime'],self.deployment(new)['spec']['template']['metadata']['annotations']['checksum/runtime'])
class KafkaOptionSourceTests(unittest.TestCase):
    def result(self, body):
        files = compatible_files()
        files['app/jobs/kafka.py'] = body
        return source.inspect(files, 'b' * 40)

    def function(self, statements):
        import textwrap
        return 'def connection_options(cfg):\n' + textwrap.indent(statements, '    ')

    def keyword_update(self, name='options'):
        return name + '.update(ssl_context=cfg.context, sasl_mechanism=cfg.mechanism, sasl_plain_username=cfg.username, sasl_plain_password=cfg.password)\n'

    def assert_blocked(self, body):
        report = self.result(body)
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('kafkaClientOptions', report['missing'])
        self.assertFalse(report['runtimeVerified'])

    def test_actual_ai_main_keyword_fixture_is_compatible(self):
        body = (ROOT / 'tests/ci/v3/ai-connection-options-keyword.fixture.py').read_text()
        report = self.result(body)
        self.assertEqual(report['status'], 'compatible')
        self.assertEqual(report['missing'], [])
        self.assertFalse(report['runtimeVerified'])
        self.assertEqual(set(report), {'kind','source','status','checks','missing','files','runtimeVerified'})
        self.assertEqual(report['source'], 'b' * 40)
        import hashlib
        self.assertEqual(report['files']['app/jobs/kafka.py'], hashlib.sha256(body.encode()).hexdigest())

    def test_literal_update_and_local_aliases_remain_supported(self):
        expressions = [
            'options = {}\n' + self.keyword_update() + 'return options\n',
            'options = {}\noptions.update({"ssl_context":cfg.context,"sasl_mechanism":cfg.mechanism,"sasl_plain_username":cfg.username,"sasl_plain_password":cfg.password})\nreturn options\n',
            'options: dict = {}\nalias = options\n' + self.keyword_update('alias') + 'return options\n',
            'options = {}\n' + self.keyword_update() + 'options: dict\nreturn options\n',
        ]
        for body in expressions:
            with self.subTest(body=body):
                self.assertEqual(self.result(self.function(body))['status'], 'compatible')

    def test_sasl_branch_accepts_plaintext_early_return(self):
        body = 'options = {}\nif cfg.KAFKA_SECURITY_PROTOCOL == "PLAINTEXT":\n    return options\n' + self.keyword_update() + 'return options\n'
        self.assertEqual(self.result(self.function(body))['status'], 'compatible')
        body = 'options = {}\nif "SASL_SSL" == cfg.KAFKA_SECURITY_PROTOCOL:\n    ' + self.keyword_update() + 'return options\n'
        self.assertEqual(self.result(self.function(body))['status'], 'compatible')

    def test_every_security_keyword_is_required(self):
        body = (ROOT / 'tests/ci/v3/ai-connection-options-keyword.fixture.py').read_text()
        for key in source.SECURITY_OPTIONS:
            modified = '\n'.join(line for line in body.splitlines() if not line.lstrip().startswith(key + '=')) + '\n'
            with self.subTest(key=key):self.assert_blocked(modified)

    def test_comments_strings_and_unused_dict_do_not_pass(self):
        for unused in [
            repr(' '.join(sorted(source.SECURITY_OPTIONS))) + '\n',
            '{"ssl_context":1,"sasl_mechanism":1,"sasl_plain_username":1,"sasl_plain_password":1}\n',
            'unused = {}\n' + self.keyword_update('unused')]:
            with self.subTest(unused=unused):self.assert_blocked(self.function(unused + 'return {}\n'))

    def test_other_dictionary_or_update_result_is_not_the_return_value(self):
        for body in [
            'options = {}\nother = {}\n' + self.keyword_update('other') + 'return options\n',
            'options = {}\nreturn ' + self.keyword_update(),
            'options = {}\nalias = options\noptions = {}\n' + self.keyword_update('alias') + 'return options\n',
            'options = {}\n' + self.keyword_update() + 'options = {}\nreturn options\n']:
            with self.subTest(body=body):self.assert_blocked(self.function(body))

    def test_none_overwrite_removes_required_option(self):
        for key in source.SECURITY_OPTIONS:
            for update in [f'options.update({key}=None)\n', f'options.update({{"{key}":None}})\n']:
                body = 'options = {}\n' + self.keyword_update() + update + 'return options\n'
                with self.subTest(key=key, update=update):self.assert_blocked(self.function(body))
        self.assert_blocked(self.function('return {"ssl_context":cfg.context,"sasl_mechanism":cfg.mechanism,"sasl_plain_username":cfg.username,"sasl_plain_password":cfg.password,"ssl_context":None}\n'))

    def test_unreachable_or_plaintext_only_updates_do_not_pass(self):
        for condition in ['False', 'cfg.KAFKA_SECURITY_PROTOCOL == "PLAINTEXT"']:
            body = 'options = {}\nif ' + condition + ':\n    ' + self.keyword_update() + 'return options\n'
            with self.subTest(condition=condition):self.assert_blocked(self.function(body))

    def test_unknown_condition_must_have_security_on_both_return_paths(self):
        body = 'options = {}\nif cfg.unknown:\n    ' + self.keyword_update() + 'return options\n'
        self.assert_blocked(self.function(body))
        body = 'options = {}\nif cfg.unknown:\n    ' + self.keyword_update() + 'else:\n    ' + self.keyword_update() + 'return options\n'
        self.assertEqual(self.result(self.function(body))['status'], 'compatible')

    def test_early_missing_return_and_implicit_none_are_rejected(self):
        for ending in ['if cfg.unknown:\n    return {}\nreturn options\n', 'if cfg.unknown:\n    return options\n']:
            self.assert_blocked(self.function('options = {}\n' + self.keyword_update() + ending))

    def test_dynamic_unpacking_and_key_mutations_are_blocked(self):
        for mutation in ['options.update(**cfg.extra)\n', 'options[cfg.key] = None\n', 'options.clear()\n', 'del options["ssl_context"]\n', 'options.update(cfg.extra)\n']:
            with self.subTest(mutation=mutation):self.assert_blocked(self.function('options = {}\n' + self.keyword_update() + mutation + 'return options\n'))

    def test_mutable_mapping_escape_cannot_retain_stale_keys(self):
        for mutation in ['mutate(options)\n', 'result = mutate(options) or None\n', 'assert mutate(options)\n', 'if mutate(options):\n    pass\n', 'options.update(ssl_context=mutate(options))\n', 'options.update({"ssl_context":mutate(options)})\n']:
            with self.subTest(mutation=mutation):self.assert_blocked(self.function('options = {}\n' + self.keyword_update() + mutation + 'return options\n'))

    def test_protocol_input_reassignment_is_not_assumed_sasl(self):
        self.assert_blocked(self.function('cfg = other_config()\noptions = {}\nif cfg.KAFKA_SECURITY_PROTOCOL == "SASL_SSL":\n    ' + self.keyword_update() + 'return options\n'))

    def test_nested_function_and_other_scope_do_not_count(self):
        self.assert_blocked('def outer():\n    def connection_options(cfg):\n        return {"ssl_context":1,"sasl_mechanism":1,"sasl_plain_username":1,"sasl_plain_password":1}\n')

    def test_unsupported_flow_is_bounded_and_never_executed(self):
        self.assert_blocked(self.function('raise RuntimeError("must not execute inspected source")\n'))
        self.assert_blocked(self.function('options = {}\n' + self.keyword_update() + 'for item in cfg.values:\n    options.clear()\nreturn options\n'))
        self.assert_blocked(self.function('options = {}\n' + self.keyword_update() + ('if cfg.unknown:\n    pass\n' * 7) + 'return options\n'))

    def test_keyword_support_does_not_bypass_other_checks(self):
        files = compatible_files()
        files['app/jobs/kafka.py'] = (ROOT / 'tests/ci/v3/ai-connection-options-keyword.fixture.py').read_text()
        files['app/infra/qdrant.py'] = 'a=QdrantClient(url=location,api_key=key)\nb=AsyncQdrantClient(url=location)\n'
        report = source.inspect(files, 'b' * 40)
        self.assertTrue(report['checks']['kafkaClientOptions'])
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('qdrantClients', report['missing'])


    def test_keyword_screen_preserves_image_and_shared_model_review_gates(self):
        values = ai.fixture()
        files = compatible_files()
        files['app/jobs/kafka.py'] = (ROOT / 'tests/ci/v3/ai-connection-options-keyword.fixture.py').read_text()
        screen = source.inspect(files, values['source'])
        image = {'image':values['image'],'source':values['source'],'runAsUser':10001,'registryDigestVerified':True,'readOnlyRootSupported':True,'minimumMemoryReviewed':True,'healthDependenciesAvailable':True}
        models = {'sharedCapacityApproved':True,'routeAndSecurityGroupVerified':True,'privatePeers':[]}
        result = prepare.prepare(values['image'],values['source'],screen,image,models,values['trust'],'1'*32)
        self.assertTrue(result['full.values.json']['review']['sourceCompatible'])
        self.assertFalse(screen['runtimeVerified'])
        with self.assertRaises(ValueError):
            prepare.prepare(values['image'],values['source'],screen,image|{'registryDigestVerified':False},models,values['trust'],'1'*32)
        with self.assertRaises(ValueError):
            prepare.prepare(values['image'],values['source'],screen,image,models|{'sharedCapacityApproved':False},values['trust'],'1'*32)

    def test_opaque_or_redefined_connection_function_is_not_assumed_dict(self):
        valid = self.function('options = {}\n' + self.keyword_update() + 'return options\n')
        self.assert_blocked('@wrapper\n' + valid)
        self.assert_blocked(valid + '\ndef connection_options(cfg):\n    return {}\n')
        self.assert_blocked(self.function('options = {}\n' + self.keyword_update() + 'yield options\nreturn options\n'))
        self.assert_blocked(self.function('options = {}\nif (cfg := other()):\n    pass\n' + self.keyword_update() + 'return options\n'))

if __name__=='__main__':unittest.main()
