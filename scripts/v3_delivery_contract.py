"""Shared immutable full-app delivery boundaries. No live clients."""
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
UNITS = ['frontend','auth-api','trip-api','photo-api','photo-converter','trip-result-worker','trip-websocket','ai-api','ai-runner']
NAMESPACES = ['frontend','auth','trip','photo','ai','kafka']
OIDC_SUBJECT = 'repo:100-hours-a-week@167328634/KTB4-2nd-Cloud@1344655631:environment:v3-production'
COMPONENTS = {
 'frontend': ('100-hours-a-week/KTB4-2nd-FE','yeodam-frontend','frontend-image.yml'),
 'backend': ('100-hours-a-week/KTB4-2nd-BE','yeodam-backend','backend-image.yml'),
 'websocket': ('100-hours-a-week/KTB4-2nd-BE','yeodam-websocket','production-websocket-image.yml'),
 'ai-app': ('100-hours-a-week/KTB4-2nd-AI','yeodam-ai-app','ai-v2-image.yml'),
}
APP_NAMES = {'frontend':'frontend','auth-api':'auth','trip-api':'trip','photo-api':'photo',
             'photo-converter':'photo','trip-result-worker':'trip','trip-websocket':'trip','ai-api':'ai','ai-runner':'ai'}
WAVES = {'ai-api':0,'ai-runner':10,'auth-api':10,'trip-api':10,'photo-api':10,
         'photo-converter':20,'trip-result-worker':20,'trip-websocket':20,'frontend':30}
CHARTS = ['apps','ai-apps','service-access']
PROFILE_ROLES = {'frontend':['frontend'],'backend':UNITS[1:6],'websocket':['trip-websocket']}
RESOURCES = {
 'frontend': {'deployments':['frontend'],'services':['frontend'],'configmaps':['frontend-runtime']},
 'auth': {'deployments':['auth-api'],'services':['auth-api'],'configmaps':['auth-api-runtime']},
 'trip': {'deployments':['trip-api','trip-result-worker','trip-websocket'],'services':['trip-api','trip-websocket'],
          'configmaps':['trip-api-runtime','trip-result-worker-runtime','trip-websocket-runtime'],
          'networkpolicies':['allow-trip-api-to-ai','allow-trip-result-worker-to-ai','allow-be-to-ai-service','allow-websocket-to-trip-service']},
 'photo': {'deployments':['photo-api','photo-converter'],'services':['photo-api'],'configmaps':['photo-api-runtime','photo-converter-runtime'],
           'networkpolicies':['allow-photo-api-to-ai','allow-be-to-ai-service']},
 'ai': {'deployments':['ai-api','ai-runner'],'services':['ai-api'],'configmaps':['ai-trust','ai-runtime-scripts','ai-api-runtime','ai-runner-runtime'],
        'serviceaccounts':['ai-api','ai-runner'],'externalsecrets':['ai-runtime'],
        'networkpolicies':['allow-ai-kafka','allow-ai-public-and-identity','allow-ai-storage','allow-be-to-ai-api','allow-ai-reviewed-private-models']},
 'kafka': {'networkpolicies':['allow-kafka-from-ai']},
}
GROUPS = {'deployments':'apps','services':'','configmaps':'','serviceaccounts':'',
          'externalsecrets':'external-secrets.io','networkpolicies':'networking.k8s.io'}
KINDS = {'deployments':'Deployment','services':'Service','configmaps':'ConfigMap','serviceaccounts':'ServiceAccount',
         'externalsecrets':'ExternalSecret','networkpolicies':'NetworkPolicy'}


def role_rules(namespace):
 # The pinned cache retains a kind if ANY managed namespace permits it, then
 # lists that kind in ALL managed namespaces. Keep reads uniform, writes scoped.
 rules=[{'apiGroups':[group],'resources':[resource],'verbs':['get','list','watch']}
        for resource,group in GROUPS.items()]
 for resource,names in RESOURCES[namespace].items():
  group=GROUPS[resource]
  rules += [{'apiGroups':[group],'resources':[resource],'verbs':['create']},
            {'apiGroups':[group],'resources':[resource],'resourceNames':names,'verbs':['update','patch']}]
 rules += [{'apiGroups':[''],'resources':['pods','events'],'verbs':['get','list','watch']},
           {'apiGroups':['apps'],'resources':['replicasets'],'verbs':['get','list','watch']}]
 return rules


def project_kinds():
 return [{'group':GROUPS[k],'kind':KINDS[k]}for k in GROUPS]
