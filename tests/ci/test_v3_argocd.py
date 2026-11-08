import copy
import importlib.util
import os
from pathlib import Path
import subprocess
import unittest
import yaml

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('argocd_checks',ROOT/'scripts/check-v3-argocd.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class ArgoContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.access=m.platform.load_documents(m.render_access(enabled=True,appsAdoptionReviewed=True))
        archive=Path(os.environ['V3_HELM']).parent/'argocd.tgz'
        text=subprocess.check_output([os.environ['V3_HELM'],'template','argocd',str(archive),'-n','argocd','--include-crds','-f',str(m.ARG/'values.yaml')],text=True)
        cls.controller=m.platform.load_documents(text)

    def test_empty_by_default_and_explicit_adoption(self):
        self.assertEqual(m.platform.load_documents(m.render_access()),[])
        m.validate_access(self.access,True)
        m.validate_controller(self.controller)

    def test_sync_before_access_or_adoption_is_rejected(self):
        for values in [{'automatedSync':True},{'appsAdoptionReviewed':True},{'enabled':True,'automatedSync':True},{'enabled':'invalid'},{'unknown':True}]:
            with self.subTest(values=values), self.assertRaises(subprocess.CalledProcessError):m.render_access(**values)

    def test_main_tracking_prune_selfheal_or_finalizer_rejected(self):
        for field,value in [('branch','main'),('prune',True),('selfHeal',True),('enabled',True),('finalizer',['resources-finalizer.argocd.argoproj.io'])]:
            docs=copy.deepcopy(self.access);app=next(x for x in docs if x['kind']=='Application')
            if field=='branch':app['spec']['source']['targetRevision']=value
            elif field=='finalizer':app['metadata']['finalizers']=value
            else:app['spec']['syncPolicy']['automated'][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):m.validate_access(docs,True)

    def test_project_cannot_own_secrets_data_or_other_namespace(self):
        for mode in ['namespace','secret','cluster','repository']:
            docs=copy.deepcopy(self.access);project=next(x for x in docs if x['kind']=='AppProject')['spec']
            if mode=='namespace':project['destinations'][0]['namespace']='mysql'
            elif mode=='secret':project['namespaceResourceWhitelist'].append({'group':'','kind':'Secret'})
            elif mode=='cluster':project['clusterResourceWhitelist']=[{'group':'*','kind':'*'}]
            else:project['sourceRepos']=['*']
            with self.subTest(mode=mode),self.assertRaises(ValueError):m.validate_access(docs,True)

    def test_kubernetes_role_is_a_separate_write_boundary(self):
        for mode in ['delete','secret','subject']:
            docs=copy.deepcopy(self.access)
            if mode=='subject':next(x for x in docs if x['kind']=='RoleBinding')['subjects'][0]['namespace']='default'
            else:
                rule=next(x for x in docs if x['kind']=='Role')['rules'][0]
                if mode=='delete':rule['verbs'].append('delete')
                else:rule['resources'].append('secrets')
            with self.subTest(mode=mode),self.assertRaises(ValueError):m.validate_access(docs,True)

    def test_no_cost_or_permissions_widening(self):
        for mode in ['replicas','memory','image','public','scope']:
            docs=copy.deepcopy(self.controller)
            obj=next(x for x in docs if x['kind']=='Deployment' and x['metadata']['name']=='argocd-server')
            if mode=='replicas':obj['spec']['replicas']=2
            elif mode=='memory':obj['spec']['template']['spec']['containers'][0]['resources']['limits']['memory']='2Gi'
            elif mode=='image':obj['spec']['template']['spec']['containers'][0]['image']='argocd:latest'
            elif mode=='public':next(x for x in docs if x['kind']=='Service')['spec']['type']='LoadBalancer'
            else:next(x for x in docs if x['kind']=='Secret' and x['metadata']['name']=='argocd-cluster-v3-apps')['stringData']['namespaces']='frontend,mysql'
            with self.subTest(mode=mode),self.assertRaises(ValueError):m.validate_controller(docs)

    def test_workflow_has_no_push_trigger_and_write_only_after_environment(self):
        workflow=yaml.safe_load((ROOT/'.github/workflows/v3-approved-release.yml').read_text())
        self.assertEqual(set(workflow.get('on',workflow.get(True))),{'workflow_dispatch'})
        self.assertEqual(workflow['permissions']['contents'],'read')
        publish=workflow['jobs']['publish']
        self.assertEqual(publish['environment'],'v3-production')
        self.assertEqual(publish['if'],"needs.preview.outputs.publication_enabled == 'true'")
        self.assertEqual(publish['permissions']['contents'],'write')
        self.assertEqual(publish['permissions']['id-token'],'write')
        self.assertNotIn('id-token',workflow['jobs']['preview'].get('permissions',workflow['permissions']))
        for job in workflow['jobs'].values():
            for step in job['steps']:
                if step.get('uses','').startswith('actions/checkout@'):self.assertFalse(step['with']['persist-credentials'])
                if 'run' in step:
                    subprocess.run(['bash','-n'],input=step['run'],text=True,check=True)
                    for forbidden in ['aws ', 'kubectl ', 'helm upgrade', 'terraform apply']:
                        self.assertNotIn(forbidden,step['run'])
