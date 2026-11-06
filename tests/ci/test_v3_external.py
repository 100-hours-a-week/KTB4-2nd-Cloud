"""FE, Controller and ALB routing checks without AWS or live workload credentials."""
from copy import deepcopy
from fnmatch import fnmatchcase
import importlib.util,json,os,subprocess,tempfile,unittest
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[2]
def module(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/file);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
apps=module('fe_apps','check-v3-apps.py');external=module('external','check-v3-external.py');plan=module('ingress_plan','check-v3-ingress-plan.py');prepare=module('external_prepare','prepare-v3-external-values.py')
PROFILE=ROOT/'deploy/v3/environments/foundation/frontend.values.yaml'
FIXTURE={'images':{'frontend':'ghcr.io/100-hours-a-week/yeodam-frontend@sha256:'+'7'*64},'defaults':{'configurationRevision':'c'*64,'configMapData':{'RELEASE':'5322c01311f9b3194805bce05c5b2d6418aa4765'}},'workloads':{'frontend':{'enabled':True}}}
ACCESS={'backendRoutesEnabled':True,'controllerPolicyEnabled':True,'ingressEnabled':True,'certificateArn':'arn:aws:acm:ap-northeast-2:483175530259:certificate/00000000-0000-0000-0000-000000000001','albSecurityGroupId':'sg-'+'1'*17}
class External(unittest.TestCase):
    def fe(self,override=None):
        with tempfile.TemporaryDirectory() as directory:
            files=[PROFILE]
            for name,value in [('fixture',FIXTURE),('override',override or {})]:
                p=Path(directory)/(name+'.yaml');p.write_text(yaml.safe_dump(value));files.append(p)
            source=apps.render(files);values=apps.load_values(files);docs=apps.platform.load_documents(source);apps.validate(docs,values);return source,docs,values
    def rejects(self,override):
        with self.assertRaises((ValueError,subprocess.CalledProcessError)):self.fe(override)
    def access(self,values=ACCESS):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'values.json';p.write_text(json.dumps(values));source=external.render_access([p])
        docs=external.platform.load_documents(source);external.validate_access(docs,values);return source,docs
    def test_disabled_and_policy_only_create_no_ingress(self):
        self.assertFalse(apps.platform.load_documents(apps.render([PROFILE])))
        self.assertEqual(self.access({'backendRoutesEnabled':False,'controllerPolicyEnabled':False,'ingressEnabled':False,'certificateArn':'','albSecurityGroupId':''})[1],[])
        docs=self.access({'backendRoutesEnabled':False,'controllerPolicyEnabled':True,'ingressEnabled':False,'certificateArn':'','albSecurityGroupId':''})[1]
        self.assertEqual([d['kind'] for d in docs],['NetworkPolicy'])
    def test_initial_routes_do_not_reference_missing_trip_photo_services(self):
        values=dict(ACCESS,backendRoutesEnabled=False)
        source,docs=self.access(values);external.platform.schema_check(source)
        ingress=[d for d in docs if d['kind']=='Ingress']
        self.assertEqual(len(ingress),5)
        services={p['backend']['service']['name'] for d in ingress for p in d['spec']['rules'][0]['http']['paths']}
        self.assertEqual(services,{'frontend','auth-api','trip-websocket','reject'})
    def test_fe_minimum_readonly_cache_and_service_first(self):
        source,docs,_=self.fe();apps.platform.schema_check(source)
        d=next(d for d in docs if d['kind']=='Deployment');pod=d['spec']['template']['spec'];c=pod['containers'][0]
        self.assertEqual(c['resources']['requests']['memory'],'128Mi');self.assertEqual(c['resources']['limits']['memory'],'512Mi')
        self.assertTrue(c['securityContext']['readOnlyRootFilesystem'])
        self.assertIn({'name':'next-cache','mountPath':'/app/.next/cache'},c['volumeMounts'])
        self.assertEqual(c['readinessProbe']['httpGet']['path'],'/login')
        self.assertFalse(pod.get('initContainers'))
        staged=self.fe({'frontendServiceOnly':True})[1]
        self.assertEqual({d['kind'] for d in staged},{'Service','ConfigMap'})
        self.assertEqual(next(d for d in staged if d['kind']=='Service'),next(d for d in docs if d['kind']=='Service'))
    def test_wrong_image_budget_runtime_or_scope_rejected(self):
        for override in [{'images':{'frontend':'ghcr.io/100-hours-a-week/yeodam-backend@sha256:'+'7'*64}},
                         {'defaults':{'resources':{'limits':{'memory':'1Gi'}}}},
                         {'defaults':{'configMapData':{'SERVER_API_BASE_URL':'http://backend.production.yeodam.internal/api'}}},
                         {'defaults':{'configMapData':{'NEXT_PUBLIC_KAKAO_MAP_JS_KEY':'must-be-build-time'}}},
                         {'defaults':{'probes':{'readiness':{'httpGet':{'path':'/made-up-health'}}}}},
                         {'workloads':{'auth-api':{'enabled':True}}}, {'websocketContract':True}]:
            with self.subTest(override=override):self.rejects(override)
    def test_actual_controller_and_public_artifact(self):
        directory=Path(os.environ['V3_HELM']).parent;docs=external.render_controller(directory);external.validate_controller(docs);external.controller_schemas(docs)
        public=[d for d in docs if d['kind'] not in ['Secret','CustomResourceDefinition']]
        external.platform.schema_check(yaml.safe_dump_all(public))
        self.assertFalse(any(d['kind']=='Secret' for d in public))
        changed=deepcopy(docs);next(d for d in changed if d['kind']=='Deployment')['spec']['replicas']=2
        with self.assertRaises(ValueError):external.validate_controller(changed)
    def test_controller_does_not_mutate_service_or_own_security_groups(self):
        values=yaml.safe_load((external.CONTROLLER/'values.yaml').read_text())
        self.assertFalse(values['enableServiceMutatorWebhook']);self.assertFalse(values['enableBackendSecurityGroup']);self.assertFalse(values['clusterSecretsPermissions']['allowAllSecrets'])
        policy=json.loads((ROOT/'terraform/v3/ingress/policies/controller.json').read_text())
        actions=[a for s in policy['Statement'] for a in (s['Action'] if isinstance(s['Action'],list) else [s['Action']])]
        self.assertTrue(all(not a.startswith('ec2:') or a.startswith(('ec2:Describe','ec2:Get')) for a in actions))
        self.assertFalse(any(a.startswith(('secretsmanager:','ssm:','ec2:Create','ec2:Authorize','ec2:Revoke','ec2:Delete')) for a in actions))
        for s in policy['Statement']:
            for tag in ['aws:RequestTag/elbv2.k8s.aws/cluster','aws:ResourceTag/elbv2.k8s.aws/cluster']:
                if tag in s.get('Condition',{}).get('StringEquals',{}):self.assertEqual(s['Condition']['StringEquals'][tag],'yeodam-v3-foundation')
    def test_access_schema_and_namespace_local_targets(self):
        source,docs=self.access();external.platform.schema_check(source)
        ingresses=[d for d in docs if d['kind']=='Ingress'];self.assertEqual(len(ingresses),7)
        self.assertEqual({d['spec']['ingressClassName'] for d in ingresses},{'yeodam-v3-alb'})
        self.assertEqual({d['metadata']['annotations']['alb.ingress.kubernetes.io/group.name'] for d in ingresses},{'yeodam-v3-validation'})
        self.assertEqual({d['spec']['rules'][0]['host'] for d in ingresses},{'v3.yeodam-2gether.com'})
    def test_ingress_scope_and_policy_widening_rejected(self):
        _,docs=self.access()
        for failure in ['host','order','source','target','tls']:
            changed=deepcopy(docs);ingress=next(d for d in changed if d['kind']=='Ingress')
            if failure=='host':ingress['spec']['rules'][0]['host']='yeodam-2gether.com'
            if failure=='order':ingress['metadata']['annotations']['alb.ingress.kubernetes.io/group.order']='999'
            if failure=='source':next(d for d in changed if d['kind']=='NetworkPolicy')['spec']['podSelector']={}
            if failure=='target':ingress['spec']['rules'][0]['http']['paths'][0]['backend']['service']['name']='trip-api'
            if failure=='tls':ingress['metadata']['annotations'].pop('alb.ingress.kubernetes.io/ssl-redirect')
            with self.subTest(failure=failure),self.assertRaises(ValueError):external.validate_access(changed,ACCESS)
    def test_route_priority_model_matches_reviewed_controller_paths(self):
        def route(path):
            for r in sorted(external.ROUTES['routes'],key=lambda r:r['order']):
                for p,t in r['paths']:
                    if (t=='Exact' and p==path) or (t=='Prefix' and (p=='/' or path==p or path.startswith(p.rstrip('/')+'/'))) or (t=='ImplementationSpecific' and fnmatchcase(path,p)):
                        return r.get('action',r.get('service'))
        cases={'/api/internal/websocket/authentication':403,'/api/actuator/health':403,'/actuator/health':403,'/metrics':403,
               '/api/ws':'trip-websocket','/api/auth/kakao/callback':'auth-api','/api/users/me':'auth-api',
               '/api/trips/1/attachments/upload-urls':'photo-api','/api/trips/1/initial-attachments':'photo-api',
               '/api/trips/1/place-folders/2/attachments':'photo-api','/api/trips/1/unclassified-folders/unknown/attachments':'photo-api',
               '/api/attachments/1/download':'photo-api','/api/trips/1':'trip-api','/api/stories/1':'trip-api','/api/ai/runpod/status':'trip-api',
               '/api/unknown':404,'/login':'frontend','/trips/1':'frontend','/_next/static/a.js':'frontend'}
        for path,target in cases.items():
            with self.subTest(path=path):self.assertEqual(route(path),target)
    def test_namespace_gate_labels_exclude_ai_and_preserve_service_accounts(self):
        args=[os.environ['V3_HELM'],'template','yeodam-v3-platform',str(ROOT/'deploy/v3/platform'),'--kube-version','1.36.0','-f',str(ROOT/'deploy/v3/environments/foundation/ingress-platform.values.yaml')]
        source=subprocess.check_output(args,text=True);docs=apps.platform.load_documents(source);values=yaml.safe_load((ROOT/'deploy/v3/platform/values.yaml').read_text());values['albReadinessGates']=True
        apps.platform.validate_platform(docs,values);apps.platform.schema_check(source)
        self.assertEqual({d['metadata']['name'] for d in docs if d['kind']=='Namespace' and 'elbv2.k8s.aws/pod-readiness-gate-inject' in d['metadata']['labels']},{'frontend','auth','trip','photo'})
    def test_public_prepare_has_no_secret_values_and_preserves_issuer(self):
        out={k:{'value':v} for k,v in {'host':'v3.yeodam-2gether.com','alb_security_group_id':ACCESS['albSecurityGroupId'],'certificate_arn':ACCESS['certificateArn']}.items()}
        result=prepare.prepare(out,FIXTURE['images']['frontend'],FIXTURE['defaults']['configMapData']['RELEASE'],'c'*64)
        self.assertTrue(result['frontend-service.values.json']['frontendServiceOnly'])
        self.assertFalse(result['ingress.values.json']['backendRoutesEnabled'])
        self.assertTrue(result['ingress-full.values.json']['backendRoutesEnabled'])
        self.assertNotIn('JWT_ISSUER',result['backend-origin.values.json']['defaults']['configMapData'])
        self.assertEqual(result['backend-origin.values.json']['defaults']['configMapData']['KAKAO_REDIRECT_URI'],'https://v3.yeodam-2gether.com/api/auth/kakao/callback')
        out['host']['value']='yeodam-2gether.com'
        with self.assertRaises(ValueError):prepare.prepare(out,FIXTURE['images']['frontend'],'a'*40,'c'*64)
    def test_plan_guard_rejects_operating_resources_and_partial_plans(self):
        rows=[]
        for address in plan.BASE:
            after={}
            if address=='aws_acm_certificate.validation[0]':after={'domain_name':'v3.yeodam-2gether.com'}
            if address=='aws_security_group.alb[0]':after={'vpc_id':'vpc-0ecc3132f3b2170e9','name':'yeodam-v3-foundation-public-alb'}
            rows.append({'address':address,'mode':'managed','change':{'actions':['create'],'after':after}})
        self.assertEqual(plan.validate({'complete':True,'resource_changes':rows})['creates'],17)
        for value in [{'complete':False}, {'complete':True,'resource_changes':rows[:-1]}, {'complete':True,'resource_changes':[{'address':'aws_instance.gpu','mode':'managed','change':{'actions':['delete'],'after':{}}}]}]:
            with self.assertRaises(ValueError):plan.validate(value)
        waf=[{'address':'aws_wafv2_web_acl.validation[0]','mode':'managed','change':{'actions':['create'],'after':{'name':'yeodam-v3-validation','scope':'REGIONAL'}}},{'address':'aws_wafv2_web_acl_association.validation[0]','mode':'managed','change':{'actions':['create'],'after':{'resource_arn':'arn:aws:elasticloadbalancing:ap-northeast-2:483175530259:loadbalancer/app/yeodam-v3-validation/1234567890abcdef'}}}]
        self.assertEqual(plan.validate({'complete':True,'resource_changes':waf})['creates'],2)
        waf[1]['change']['after']['resource_arn']='arn:aws:elasticloadbalancing:ap-northeast-2:483175530259:loadbalancer/app/production/1234567890abcdef'
        with self.assertRaises(ValueError):plan.validate({'complete':True,'resource_changes':waf})
        dns={'address':'aws_route53_record.validation[0]','mode':'managed','change':{'actions':['create'],'after':{'name':'v3.yeodam-2gether.com','allow_overwrite':False}}}
        self.assertEqual(plan.validate({'complete':True,'resource_changes':[dns]})['creates'],1)
        dns['change']['after']['name']='yeodam-2gether.com'
        with self.assertRaises(ValueError):plan.validate({'complete':True,'resource_changes':[dns]})
if __name__=='__main__':unittest.main()
