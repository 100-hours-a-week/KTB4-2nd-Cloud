#!/usr/bin/env python3
"""Offline Argo bootstrap/FE adoption checks; never connect to Kubernetes or AWS."""
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tarfile
import yaml

ROOT = Path(__file__).resolve().parents[1]
ARG = ROOT / 'deploy/v3/argocd'
ACCESS = ROOT / 'deploy/v3/argocd-access'
LOCK = json.loads((ARG / 'upstream.lock.json').read_text())
spec = importlib.util.spec_from_file_location('platform', ROOT / 'scripts/check-v3-platform.py')
platform = importlib.util.module_from_spec(spec); spec.loader.exec_module(platform)
require = platform.require


def render_access(**values):
    command = [os.environ['V3_HELM'], 'template', 'yeodam-v3-argocd-access', str(ACCESS), '-n', 'argocd', '--kube-version', '1.36.0']
    for key, value in values.items():command += ['--set', key + '=' + str(value).lower()]
    return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL)


def validate_access(docs, adopted=False, automated=False):
    require(Counter(x['kind'] for x in docs) == Counter({'Namespace': 1, 'NetworkPolicy': 2, 'Role': 1, 'RoleBinding': 1} | ({'Application': 1, 'AppProject': 1} if adopted else {})), 'Unexpected Argo access inventory')
    for obj in docs:
        kind = obj['kind']; ns = obj['metadata'].get('namespace')
        require(ns == (None if kind == 'Namespace' else 'frontend' if kind in ['Role','RoleBinding'] else 'argocd'), 'Argo ownership namespace widened')
    namespace = next(x for x in docs if x['kind']=='Namespace')['metadata']
    require(namespace['name']=='argocd' and namespace['annotations']['helm.sh/resource-policy']=='keep' and namespace['labels']['pod-security.kubernetes.io/enforce']=='restricted', 'Retained restricted argocd namespace required')
    role = next(x for x in docs if x['kind'] == 'Role')
    rules = role['rules']
    require(rules == [
        {'apiGroups':['apps'],'resources':['deployments'],'verbs':['get','list','watch','create','update','patch']},
        {'apiGroups':[''],'resources':['services','configmaps'],'verbs':['get','list','watch','create','update','patch']},
        {'apiGroups':[''],'resources':['pods','events'],'verbs':['get','list','watch']}], 'No Secret, deletion, data or cluster permissions')
    binding = next(x for x in docs if x['kind'] == 'RoleBinding')
    require(binding['roleRef']=={'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':'argocd-frontend'}, 'No ClusterRole or unrelated role binding')
    require(binding['subjects'] == [{'kind':'ServiceAccount','name':'argocd-application-controller','namespace':'argocd'}], 'Only the Argo controller can sync FE')
    if adopted:
        app = next(x for x in docs if x['kind'] == 'Application')
        require(not app['metadata'].get('finalizers'), 'Application deletion must not cascade')
        s = app['spec']
        require(s['project'] == 'yeodam-v3-frontend' and s['source'] == {'repoURL':'https://github.com/100-hours-a-week/KTB4-2nd-Cloud.git','targetRevision':'deploy/v3-approved','path':'charts/apps','helm':{'releaseName':'yeodam-v3-frontend','valueFiles':['../../values/frontend.json']}}, 'Only approved branch and immutable bundled chart allowed')
        require(s['destination'] == {'server':'https://kubernetes.default.svc','namespace':'frontend'}, 'Wrong FE destination')
        require(s['syncPolicy'] == {'automated':{'enabled':automated,'prune':False,'selfHeal':False,'allowEmpty':False},'syncOptions':['FailOnSharedResource=true']}, 'No deletion or implicit self-heal')
        project = next(x for x in docs if x['kind'] == 'AppProject')['spec']
        require(project['sourceRepos'] == [s['source']['repoURL']] and project['destinations'] == [s['destination']], 'Project source/destination widened')
        require(project['clusterResourceWhitelist'] == [] and project['namespaceResourceWhitelist'] == [{'group':'apps','kind':'Deployment'},{'group':'','kind':'Service'},{'group':'','kind':'ConfigMap'}], 'Project resource ownership widened')
    policies = {x['metadata']['name']: x['spec'] for x in docs if x['kind']=='NetworkPolicy'}
    require(policies['default-deny'] == {'podSelector':{},'policyTypes':['Ingress','Egress']}, 'Argo default deny widened')
    require(policies['control-egress'] == {'podSelector':{},'policyTypes':['Egress'],'egress':[{'to':[{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':'kube-system'}},'podSelector':{'matchLabels':{'k8s-app':'kube-dns'}}}],'ports':[{'protocol':'UDP','port':53},{'protocol':'TCP','port':53}]},{'to':[{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':'argocd'}}}],'ports':[{'protocol':'TCP','port':6379},{'protocol':'TCP','port':8081},{'protocol':'TCP','port':8080}]},{'to':[{'ipBlock':{'cidr':'0.0.0.0/0'}}],'ports':[{'protocol':'TCP','port':443}]}]}, 'Only HTTPS egress, DNS and reviewed internal ports')


def validate_controller(docs):
    require(not any(x['kind'] in ['ClusterRole','ClusterRoleBinding','Ingress','PersistentVolumeClaim','HorizontalPodAutoscaler'] for x in docs), 'No cluster admin, public ingress, new disk or HPA')
    workloads = {x['metadata']['name']: x for x in docs if x['kind'] in ['Deployment','StatefulSet']}
    require(set(workloads) == {'argocd-application-controller','argocd-repo-server','argocd-server','argocd-redis','argocd-applicationset-controller'}, 'Unknown controller workload')
    resources = {'argocd-application-controller':('100m','256Mi','512Mi'), 'argocd-repo-server':('50m','128Mi','512Mi'), 'argocd-server':('25m','64Mi','256Mi'), 'argocd-redis':('10m','32Mi','128Mi'), 'argocd-applicationset-controller':('5m','32Mi','128Mi')}
    for name, obj in workloads.items():
        require(obj['metadata']['namespace']=='argocd' and obj['spec']['replicas']==(0 if 'applicationset' in name else 1), 'Single replica only; ApplicationSet remains zero')
        pod = obj['spec']['template']['spec']
        require(pod['nodeSelector']=={'kubernetes.io/os':'linux','yeodam.io/role':'system'} and not pod.get('hostNetwork'), 'Existing system nodes only')
        require(len(pod['containers'])==1, 'No unreviewed sidecars')
        for c in pod.get('initContainers',[])+pod['containers']:
            require(c['image'] == LOCK['images']['redis' if name=='argocd-redis' else 'argocd'], 'Controller image not digest pinned')
            cpu,mem,limit=resources[name]
            require(c['resources']=={'requests':{'cpu':cpu,'memory':mem},'limits':{'memory':limit}}, 'Minimum controller budget changed')
            require(c['securityContext']['allowPrivilegeEscalation'] is False and c['securityContext']['readOnlyRootFilesystem'] is True, 'Restricted controller containers required')
    jobs = [x for x in docs if x['kind']=='Job']
    require(len(jobs)==1 and jobs[0]['metadata']['name']=='argocd-redis-secret-init', 'Only bounded initial Redis credential Job')
    job = jobs[0]['spec']['template']['spec']['containers'][0]
    require(job['image']==LOCK['images']['argocd'] and job['resources']=={'requests':{'cpu':'10m','memory':'32Mi'},'limits':{'memory':'128Mi'}}, 'Bounded initial Job resources required')
    for x in docs:
        require(x['metadata'].get('namespace','argocd')=='argocd', 'Upstream resources must stay in argocd')
        if x['kind']=='Service':require(x['spec'].get('type','ClusterIP')=='ClusterIP' and not x['spec'].get('externalIPs'), 'No public service')
    cm = next(x for x in docs if x['kind']=='ConfigMap' and x['metadata']['name']=='argocd-cm')['data']
    require(cm['exec.enabled']=='false' and cm['users.anonymous.enabled']=='false' and cm['application.resourceTrackingMethod']=='annotation', 'No exec/anonymous access or Helm label tracking collision')
    secrets={x['metadata']['name']:x for x in docs if x['kind']=='Secret'}
    require(set(secrets)=={'argocd-secret','argocd-cluster-v3-frontend'} and not secrets['argocd-secret'].get('data') and not secrets['argocd-secret'].get('stringData'), 'No repository credential or admin password in render inputs')
    credentials=next(x for x in docs if x['kind']=='Secret' and x['metadata']['name']=='argocd-cluster-v3-frontend')['stringData']
    require(credentials['server']=='https://kubernetes.default.svc' and credentials['namespaces']=='frontend' and credentials['clusterResources']=='false', 'Cluster watch must stay frontend scoped')
    require(json.loads(credentials['config'])=={'tlsClientConfig':{'insecure':False}}, 'No static cluster token or insecure TLS')


def check(directory, output):
    archive=directory/'argocd.tgz'
    require(hashlib.sha256(archive.read_bytes()).hexdigest()==LOCK['sha256'], 'Argo chart checksum mismatch')
    source=subprocess.check_output([os.environ['V3_HELM'],'template','argocd',str(archive),'-n','argocd','--kube-version','1.36.0','--include-crds','-f',str(ARG/'values.yaml')],text=True)
    docs=platform.load_documents(source);validate_controller(docs)
    # CRD schemas originate inside the checksum-verified chart, never from a live cluster.
    for obj in docs:
        if obj['kind']=='CustomResourceDefinition':
            for version in obj['spec']['versions']:
                if version['name']=='v1alpha1':
                    path=Path(os.environ['V3_SCHEMA_DIR'])/(obj['spec']['names']['kind'].lower()+'.json')
                    path.write_text(json.dumps(version['schema']['openAPIV3Schema']))
    safe=[x for x in docs if x['kind']not in ['Secret','CustomResourceDefinition']]
    platform.schema_check(yaml.safe_dump_all(safe))
    output.mkdir(parents=True,exist_ok=True)
    (output/'argocd-reviewed.yaml').write_text(yaml.safe_dump_all(safe))
    require(not platform.load_documents(render_access()), 'Default bootstrap must be empty')
    for name,params,adopted,automated in [('access',{'enabled':True},False,False),('adoption',{'enabled':True,'frontendAdoptionReviewed':True},True,False),('approved-sync',{'enabled':True,'frontendAdoptionReviewed':True,'automatedSync':True},True,True)]:
        text=render_access(**params);validate_access(platform.load_documents(text),adopted,automated);platform.schema_check(text);(output/('argocd-'+name+'.yaml')).write_text(text)
    print(json.dumps({'residentPods':4,'requestsCpuMilli':185,'requestsMemoryMi':480,'limitsMemoryMi':1408,'applicationSetReplicas':0,'namespace':'argocd','defaultEnabled':False,'clusterAccessed':False,'awsAccessed':False}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();check(a.directory,a.output)
