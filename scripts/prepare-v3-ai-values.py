#!/usr/bin/env python3
"""Prepare public rollout references. Never read credentials, apply or call shared models."""
import argparse,hashlib,ipaddress,json,os,re,ssl
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def require(ok,message):
    if not ok:raise ValueError(message)
def prepare(image,source,screen,inspection,models,trust,runtime_version):
    require(re.fullmatch('[a-fA-F0-9-]{32,64}',runtime_version),'Explicit V3 runtime Secret VersionId required; never supply credential values')
    require(re.fullmatch(r'ghcr.io/100-hours-a-week/yeodam-ai-app@sha256:[a-f0-9]{64}',image),'Immutable AI app image required')
    require(re.fullmatch('[a-f0-9]{40}',source),'Exact image source required')
    require(set(screen)=={'kind','source','status','checks','missing','files','runtimeVerified'} and set(screen['checks'])=={'kafkaProtocol','kafkaSettings','kafkaClientOptions','qdrantSetting','qdrantClients','placeIndexSettings','runnerIndexPreparation'},'Exact source-screen capabilities required')
    require(set(inspection)=={'image','source','runAsUser','registryDigestVerified','readOnlyRootSupported','minimumMemoryReviewed','healthDependenciesAvailable'},'Public image review metadata only')
    require(set(models)=={'sharedCapacityApproved','routeAndSecurityGroupVerified','privatePeers'},'Public model review metadata only')
    require(screen.get('kind')=='v3-ai-source-screen-v1' and screen.get('source')==source and screen.get('status')=='compatible' and screen.get('checks') and all(screen['checks'].values()),'AI Kafka/Qdrant capability screen is blocked or source differs')
    require(inspection.get('image')==image and inspection.get('source')==source and inspection.get('runAsUser')==10001 and all(inspection.get(k) is True for k in ['registryDigestVerified','readOnlyRootSupported','minimumMemoryReviewed','healthDependenciesAvailable']),'Review the actual image, UID, memory and probe dependencies first')
    require(models.get('sharedCapacityApproved') is True and models.get('routeAndSecurityGroupVerified') is True,'Shared model capacity and network review required; no GPU modifications here')
    peers=models.get('privatePeers',[]);require(isinstance(peers,list) and len(peers)<=2,'At most two exact model endpoints')
    for p in peers:
        require(set(p)=={'cidr','port'},'Only public peer metadata')
        network=ipaddress.ip_network(p['cidr']);require(network.version==4 and network.prefixlen==32 and network.subnet_of(ipaddress.ip_network('10.0.0.0/8')) and isinstance(p['port'],int) and 1<=p['port']<=65535,'Exact private model /32 and port only')
    require(set(trust)=={'redis','qdrant','kafka'},'Three public CAs required')
    for ca in trust.values():
        require('PRIVATE KEY' not in ca and 'BEGIN CERTIFICATE' in ca,'Public CA certificates only');ssl.create_default_context(cadata=ca)
    revision=hashlib.sha256(json.dumps({'image':image,'source':source,'screen':screen,'inspection':inspection,'models':models,'trust':trust,'runtimeSecretVersionId':runtime_version},sort_keys=True).encode()).hexdigest()
    base={'accessEnabled':True,'workloads':{'api':False,'runner':False},'image':image,'source':source,'configurationRevision':revision,'review':{'sourceCompatible':True,'imageReviewed':True,'connectionsReviewed':True},'trust':trust,'privateModelPeers':peers}
    return {'access.values.json':base,'api.values.json':dict(base,workloads={'api':True,'runner':False}),'full.values.json':dict(base,workloads={'api':True,'runner':True}),'backend-ai.values.json':{'defaults':{'configMapData':{'AI_SERVER_BASE_URL':'http://ai-api.ai.svc.cluster.local:8000'}}}}
def main():
    p=argparse.ArgumentParser(description=__doc__)
    for k in ['source-screen','image-review','model-review','redis-ca','qdrant-ca','kafka-ca','directory']:p.add_argument('--'+k,required=True,type=Path)
    p.add_argument('--secret-version',required=True);p.add_argument('--image',required=True);p.add_argument('--source',required=True);a=p.parse_args();os.umask(0o077)
    values=prepare(a.image,a.source,json.loads(a.source_screen.read_text()),json.loads(a.image_review.read_text()),json.loads(a.model_review.read_text()),{n:getattr(a,n+'_ca').read_text() for n in ['redis','qdrant','kafka']},a.secret_version)
    require(not a.directory.exists(),'Refusing overwrite');a.directory.mkdir(parents=True)
    for n,v in values.items():(a.directory/n).write_text(json.dumps(v,indent=2)+'\n')
    print('Prepared public access/API/full inputs; no credentials, AWS, deployment or model calls')
if __name__=='__main__':main()
