#!/usr/bin/env python3
"""Offline one-shot V3 schema boundaries; never reads credentials or connects to databases."""
import argparse,collections,importlib.util,json,os,re,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location('platform',ROOT/'scripts/check-v3-platform.py');platform=importlib.util.module_from_spec(s);s.loader.exec_module(platform)
LOCK=json.loads((ROOT/'deploy/v3/mysql-schema-image/source.lock.json').read_text());CHART=ROOT/'deploy/v3/mysql-schema'
def require(value,message):
 if not value:raise ValueError(message)
def render(files=()):
 args=[os.environ['V3_HELM'],'template','yeodam-v3-mysql-schema',str(CHART),'-n','default','--kube-version','1.36.0']
 for f in files:args+=['-f',str(f)]
 return subprocess.check_output(args,text=True)
def validate(docs,enabled=False):
 if not enabled:require(not docs,'Disabled schema release must create zero resources');return
 require(collections.Counter(x['kind'] for x in docs)=={'Job':1,'NetworkPolicy':2},'Only one Job and two scoped policies; no DB/Secret/PVC/Node changes')
 require(all(x['metadata'].get('namespace')=='mysql' and x['metadata']['labels'].get('yeodam.io/stage')=='v3' for x in docs),'V3 mysql namespace only')
 require(len({(x['kind'],x['metadata']['name']) for x in docs})==3,'Unique resource identities required')
 job=next(x for x in docs if x['kind']=='Job');require(job['metadata']['name']=='v3-mysql-schema','Reviewed Job identity only')
 spec=job['spec'];require(set(spec)=={'backoffLimit','activeDeadlineSeconds','template'} and spec['backoffLimit']==0 and spec['activeDeadlineSeconds']==600,'No automatic migration retries')
 t=spec['template'];require(t['metadata']['labels']=={'app.kubernetes.io/instance':'mysql-schema','app.kubernetes.io/part-of':'yeodam','yeodam.io/stage':'v3'},'Dedicated migration label only')
 p=t['spec'];require(set(p)=={'restartPolicy','automountServiceAccountToken','enableServiceLinks','nodeSelector','imagePullSecrets','securityContext','containers','volumes'},'No alternate scheduling/host/service account settings')
 require(p['restartPolicy']=='Never' and p['automountServiceAccountToken'] is False and p['enableServiceLinks'] is False,'No Kubernetes API credentials or restart loop')
 require(p['nodeSelector']=={'yeodam.io/role':'system'} and p['imagePullSecrets']==[{'name':'ghcr-pull'}],'Existing system Node and explicit registry prerequisite')
 require(p['securityContext']=={'runAsNonRoot':True,'runAsUser':1001,'fsGroup':1001,'seccompProfile':{'type':'RuntimeDefault'}},'Restricted Job only')
 require(len(p['containers'])==1,'One migration process only');c=p['containers'][0]
 require(set(c)=={'name','image','resources','securityContext','volumeMounts'} and c['name']=='migrate','No command/env/config overrides')
 require(re.fullmatch('ghcr.io/100-hours-a-week/yeodam-v3-mysql-schema@sha256:[a-f0-9]{64}',c['image']),'Verified immutable schema image required')
 require(c['resources']=={'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'cpu':'500m','memory':'512Mi'}},'Minimum Job budget unchanged')
 require(c['securityContext']=={'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},'No privileges or writable image')
 require(c['volumeMounts']==[{'name':'tmp','mountPath':'/tmp'},{'name':'credentials','mountPath':'/credentials','readOnly':True},{'name':'tls','mountPath':'/tls','readOnly':True}],'No extra mounts')
 require(p['volumes']==[{'name':'tmp','emptyDir':{'sizeLimit':'32Mi'}},{'name':'credentials','secret':{'secretName':'mysql-bootstrap','defaultMode':0o440,'items':[{'key':'app-password','path':'app-password'}]}},{'name':'tls','secret':{'secretName':'mysql-bootstrap','defaultMode':0o440,'items':[{'key':'ca.crt','path':'ca.crt'}]}}],'Project only app password/public CA; never root/private TLS key')
 db={'app.kubernetes.io/name':'mysql','app.kubernetes.io/instance':'yeodam-v3-mysql'};client={'app.kubernetes.io/instance':'mysql-schema'};port=[{'protocol':'TCP','port':3306}]
 expected={'allow-v3-schema-client':{'podSelector':{'matchLabels':client},'policyTypes':['Egress'],'egress':[{'to':[{'podSelector':{'matchLabels':db}}],'ports':port},{'to':[{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':'kube-system'}},'podSelector':{'matchLabels':{'k8s-app':'kube-dns'}}}],'ports':[{'protocol':'UDP','port':53},{'protocol':'TCP','port':53}]}]},'allow-v3-schema-to-mysql':{'podSelector':{'matchLabels':db},'policyTypes':['Ingress'],'ingress':[{'from':[{'podSelector':{'matchLabels':client}}],'ports':port}]}}
 require({x['metadata']['name']:x['spec'] for x in docs if x['kind']=='NetworkPolicy'}==expected,'Only schema Job -> existing mysql and DNS, no general access')
def validate_source():
 require(LOCK['cdcTables']==['trip_creation_outbox','additional_attachment_upload_outbox','story_generation_outbox','attachment_conversion_outbox'],'Only the four reviewed Outbox tables may be granted SELECT')
 require(LOCK['sourceRepository']=='100-hours-a-week/KTB4-2nd-BE' and re.fullmatch('[a-f0-9]{40}',LOCK['sourceCommit']),'Pinned BE source required')
 require(LOCK['jdbcArtifact']['url']=='https://repo.maven.apache.org/maven2/com/mysql/mysql-connector-j/9.7.0/mysql-connector-j-9.7.0.jar' and re.fullmatch('[a-f0-9]{64}',LOCK['jdbcArtifact']['sha256']),'Pinned official MySQL JDBC required')
 require(LOCK['flywayVersion']=='12.4.0' and LOCK['schemaVersion']==34 and [int(x['name'].split('__')[0][1:]) for x in LOCK['migrations']]==list(range(1,35)),'BE Flyway version and complete migrations required')
 require(all(re.fullmatch('[a-f0-9]{64}',x['sha256']) and re.fullmatch(r'V\d+__[a-z0-9_]+\.sql',x['name']) for x in LOCK['migrations']),'Fixed named SQL hashes required')
 script=(ROOT/'deploy/v3/mysql-schema-image/migrate.sh').read_text()
 for required in ['flyway.driver=com.mysql.cj.jdbc.Driver','sslMode=VERIFY_IDENTITY','fallbackToSystemTrustStore=false','flyway.cleanDisabled=true','flyway.baselineOnMigrate=false','flyway.outOfOrder=false','flyway.validateOnMigrate=true','flyway.ignoreMigrationPatterns=','sha256sum -c','env -i','-Xmx128m']:
  require(required in script,'Missing migration safety contract: '+required)
 for forbidden in ['flyway clean','flyway repair','baselineOnMigrate=true','sslMode=REQUIRED']:
  require(forbidden not in script,'Unsafe migration behavior')
 require((ROOT/'deploy/v3/mysql-schema-image/Dockerfile').read_text().splitlines()[0]=='FROM '+LOCK['baseImage'],'Pinned Flyway base required')
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True,type=Path);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True);validate_source()
 for label,files,enabled in [('default',[],False),('foundation',[ROOT/'deploy/v3/environments/foundation/mysql-schema.values.yaml'],False),('fixture',[ROOT/'tests/ci/v3/mysql-schema.values.fixture.yaml'],True)]:
  text=render(files);validate(platform.load_documents(text),enabled);platform.schema_check(text);(a.output/('mysql-schema-'+label+'.yaml')).write_text(text)
 print('Schema contracts: default0/foundation0, explicit Job1/policies2; no database or credentials modified')
if __name__=='__main__':main()
