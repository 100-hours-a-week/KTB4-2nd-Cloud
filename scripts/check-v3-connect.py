#!/usr/bin/env python3
"""Offline Connect boundaries; no cluster, credentials, image publishing or DB collection."""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

import yaml

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('kafka',ROOT/'scripts/check-v3-kafka.py')
kafka=importlib.util.module_from_spec(spec);spec.loader.exec_module(kafka)
require=kafka.require
CHART=ROOT/'deploy/v3/connect'
IMAGE_SOURCE=ROOT/'deploy/v3/connect-image'
INTERNAL=['prod.connect-configs','prod.connect-offsets','prod.connect-status']


def render(files=()):
    args=[os.environ['V3_HELM'],'template','yeodam-v3-connect',str(CHART),'-n','default','--kube-version','1.36.0']
    for f in files:args+=['-f',str(f)]
    return subprocess.check_output(args,text=True)


def validate(documents,enabled=False,smoke=False):
    if not enabled:
        require(not documents and not smoke,'Default and unpublished foundation image must create zero objects');return
    require(Counter(d['kind'] for d in documents)=={'KafkaConnect':1,'KafkaTopic':3,'NetworkPolicy':4,**({'Job':1} if smoke else {})},'No DB Connector, Broker, Node, PVC, Secret, RBAC, Namespace or public entrypoint allowed')
    require(all(d['metadata'].get('namespace')=='kafka' and d['metadata'].get('labels',{}).get('yeodam.io/stage')=='v3' for d in documents),'V3 kafka namespace ownership required')
    require(len({(d['kind'],d['metadata']['name']) for d in documents})==len(documents),'Duplicate resource identity')
    connect=next(d for d in documents if d['kind']=='KafkaConnect');c=connect['spec']
    require(connect['metadata']['name']=='yeodam-v3-connect' and connect['metadata'].get('annotations')=={'strimzi.io/use-connector-resources':'true'},'Only the reviewed Connect identity/resource API enabled')
    require(set(c)=={'version','replicas','image','bootstrapServers','groupId','configStorageTopic','offsetStorageTopic','statusStorageTopic','authentication','tls','resources','jvmOptions','config','livenessProbe','readinessProbe','logging','template'},'No build/init/extra source or unreviewed settings')
    require(c['version']=='4.2.2' and c['replicas']==1,'One worker on the existing Kafka version')
    require(bool(re.fullmatch(r'ghcr.io/100-hours-a-week/yeodam-v3-connect@sha256:[a-f0-9]{64}',c['image'])),'Immutable reviewed private image required')
    require(c['bootstrapServers']=='yeodam-v3-kafka-kafka-bootstrap.kafka.svc:9093' and c['groupId']=='connect-v3','Isolated V3 bootstrap/group required')
    require([c[k] for k in ('configStorageTopic','offsetStorageTopic','statusStorageTopic')]==INTERNAL,'State topic names must be exact')
    require(c['authentication']=={'type':'scram-sha-512','username':'v3-connect-outbox','passwordSecret':{'secretName':'v3-connect-outbox','password':'password'}} and c['tls']=={'trustedCertificates':[{'secretName':'yeodam-v3-kafka-cluster-ca-cert','certificate':'ca.crt'}]},'TLS/SCRAM references only; no credentials in values')
    require(c['resources']=={'requests':{'cpu':'100m','memory':'512Mi'},'limits':{'cpu':'500m','memory':'768Mi'}} and c['jvmOptions']=={'-Xms':'128m','-Xmx':'256m','gcLoggingEnabled':False},'Keep the measured initial CPU/memory/heap budget')
    expected={'config.storage.replication.factor':3,'offset.storage.replication.factor':3,'status.storage.replication.factor':3,'offset.storage.partitions':1,'status.storage.partitions':1,'key.converter':'org.apache.kafka.connect.storage.StringConverter','value.converter':'org.apache.kafka.connect.storage.StringConverter','connector.client.config.override.policy':'None','offset.flush.interval.ms':60000,'producer.acks':'all','producer.enable.idempotence':True,'producer.compression.type':'lz4','ssl.endpoint.identification.algorithm':'HTTPS'}
    require(c['config']==expected,'Preserve RF3, minimal partitions, idempotent acks=all and hostname verification; no DB/file provider/override bypass')
    for name,delay,failure in [('livenessProbe',120,6),('readinessProbe',10,3)]:
        require(c[name]=={'initialDelaySeconds':delay,'periodSeconds':10,'timeoutSeconds':5,'failureThreshold':failure,'successThreshold':1},'Probe startup/failure budgets changed')
    t=c['template'];require(set(t)=={'pod','connectContainer'},'No alternate operator build or control-plane permissions')
    pod=t['pod']
    require(set(pod)=={'metadata','imagePullSecrets','affinity','enableServiceLinks','terminationGracePeriodSeconds','tmpDirSizeLimit','securityContext'},'No host mounts, tolerations, extra secrets or networking overrides')
    require(pod['affinity']==kafka.system_affinity() and pod['imagePullSecrets']==[{'name':'ghcr-pull'}],'Existing system Nodes and explicit private registry prerequisite only')
    require(pod['enableServiceLinks'] is False and pod['terminationGracePeriodSeconds']==90 and pod['tmpDirSizeLimit']=='128Mi','Bound shutdown and scratch')
    require(pod['securityContext']=={'runAsNonRoot':True,'seccompProfile':{'type':'RuntimeDefault'}} and t['connectContainer']=={'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}}},'Restricted worker required')
    topics=[d for d in documents if d['kind']=='KafkaTopic'];require({d['metadata']['name'] for d in topics}==set(INTERNAL),'Exactly three state topics')
    for d in topics:
        require(d['metadata']['labels'].get('strimzi.io/cluster')=='yeodam-v3-kafka' and d['metadata'].get('annotations')=={'helm.sh/resource-policy':'keep'},'Preserve durable state topics')
        require(d['spec']=={'topicName':d['metadata']['name'],'partitions':1,'replicas':3,'config':{'min.insync.replicas':2,'cleanup.policy':'compact','retention.ms':-1,'retention.bytes':-1,'segment.bytes':1048576,'segment.ms':600000}},'Do not expire worker offsets or change state topology')
    # Exact four policies: never replace existing default deny or Kafka Listener configuration.
    broker={'strimzi.io/cluster':'yeodam-v3-kafka','strimzi.io/kind':'Kafka'}
    worker={'strimzi.io/cluster':'yeodam-v3-connect','strimzi.io/kind':'KafkaConnect'}
    operator={'name':'strimzi-cluster-operator'};check={'app.kubernetes.io/instance':'connect-smoke'}
    def policy(selector,**rules):return {'podSelector':{'matchLabels':selector},'policyTypes':list(rules),**{k.lower():v for k,v in rules.items()}}
    def rule(peers,port,direction):return [{direction:[{'podSelector':{'matchLabels':p}} for p in peers],'ports':[{'protocol':'TCP','port':port}]}]
    expected_policies={
        'allow-v3-connect-to-brokers':policy(broker,Ingress=rule([worker],9093,'from')),
        'allow-v3-connect-runtime':policy(worker,Ingress=rule([operator,check],8083,'from'),Egress=rule([broker],9093,'to')),
        'allow-operator-connect-api':policy(operator,Egress=rule([worker],8083,'to')),
        'allow-connect-check-api':policy(check,Egress=rule([worker],8083,'to'))}
    require({d['metadata']['name']:d['spec'] for d in documents if d['kind']=='NetworkPolicy'}==expected_policies,'Only exact broker/worker/operator/check peers may communicate')
    for d in documents:
        if d['kind']=='NetworkPolicy':
            require(d['metadata']['name'].startswith(('allow-v3-connect','allow-operator-connect','allow-connect-check')),'Do not replace shared policies')
            for direction in ('ingress','egress'):
                for rule in d['spec'].get(direction,[]):
                    require(set(rule['ports'][0])=={'protocol','port'} and rule['ports'][0]['protocol']=='TCP' and rule['ports'][0]['port'] in (8083,9093),'Only Kafka and internal REST ports')
                    peers=rule.get('from',rule.get('to',[]));require(peers and all(set(p)=={'podSelector'} and p['podSelector'].get('matchLabels') for p in peers),'No namespace-wide/all-IP access')
    if smoke:
        j=next(d for d in documents if d['kind']=='Job');p=j['spec']['template']['spec'];tool=p['containers'][0]
        require(j['metadata']['name']=='v3-connect-check' and j['spec']['activeDeadlineSeconds']==120 and j['spec']['backoffLimit']==0,'Bound temporary API check')
        require(p['nodeSelector']=={'yeodam.io/role':'system'} and p['automountServiceAccountToken'] is False and p['restartPolicy']=='Never','No extra Nodes or API privileges')
        require(tool['image']==kafka.LOCK['kafkaImage'] and tool['resources']=={'requests':{'cpu':'25m','memory':'32Mi'},'limits':{'cpu':'100m','memory':'64Mi'}},'Minimal pinned public check image')
        require(set(p)=={'restartPolicy','automountServiceAccountToken','enableServiceLinks','nodeSelector','securityContext','containers','volumes'} and len(p['containers'])==1,'No extra scheduling/host/sidecar access')
        require(set(tool)=={'name','image','command','args','resources','securityContext','volumeMounts'} and tool['command']==['/bin/bash','-ec'],'No secret/env/runtime injection')
        require(tool['securityContext']=={'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},'Restricted check container')
        require(p['volumes']==[{'name':'temp','emptyDir':{'sizeLimit':'4Mi'}}] and not tool.get('envFrom'),'No credential access in the API check')
        require('/connectors' in tool['args'][0] and '/connector-plugins' in tool['args'][0] and "== '[]'" in tool['args'][0] and not any(x in tool['args'][0] for x in ('POST','PUT','DELETE','PATCH','--data','--request')),'Only read-only zero-connector/plugin checks')


def validate_image_source():
    lock=json.loads((IMAGE_SOURCE/'source.lock.json').read_text())
    require(lock['baseImage']==kafka.LOCK['kafkaImage'] and lock['kafkaVersion']==kafka.LOCK['kafkaVersion'],'Connect runtime must match pinned Strimzi Kafka version')
    require(lock['debeziumVersion']=='3.4.3.Final' and lock['groovyVersion']=='4.0.32' and len(lock['artifacts'])==4,'Only needed MySQL/script artifacts')
    for a in lock['artifacts']:
        require(a['url'].startswith('https://repo.maven.apache.org/maven2/') and re.fullmatch('[a-f0-9]{64}',a['sha256']) and a['filename']==a['url'].split('/')[-1],'Official checksummed plugin inputs only')
    lines=[l for l in (IMAGE_SOURCE/'Dockerfile').read_text().splitlines() if l and not l.startswith('#')]
    require(lines==['FROM '+lock['baseImage'],'COPY --chown=1001:0 plugins/ /opt/kafka/plugins/debezium-mysql/','USER 1001'],'No network installs, credentials or unrelated runtime image instructions')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    validate_image_source()
    archive=args.directory/'strimzi.tgz';require(hashlib.sha256(archive.read_bytes()).hexdigest()==kafka.LOCK['sha256'],'Pinned schema source checksum mismatch')
    kafka.write_custom_schemas(kafka.platform.load_documents(kafka.render(archive,kafka.OPERATOR/'values.yaml',True)))
    args.output.mkdir(parents=True,exist_ok=True)
    for label,files,enabled,smoke in [('disabled',[],False,False),('foundation',[ROOT/'deploy/v3/environments/foundation/connect.values.yaml'],False,False),('fixture',[ROOT/'tests/ci/v3/connect.values.fixture.yaml'],True,False),('smoke',[ROOT/'tests/ci/v3/connect-smoke.values.fixture.yaml'],True,True)]:
        source=render(files);validate(kafka.platform.load_documents(source),enabled,smoke);kafka.platform.schema_check(source);(args.output/f'connect-{label}.yaml').write_text(source)
    print('Connect contracts passed: default0, opt-in1Worker/3stateTopics/4policies, optional read-only Job. No DB Connector or cluster access.')


if __name__=='__main__':main()
