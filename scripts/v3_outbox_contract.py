"""Shared non-secret Outbox identities and public-only JKS validation."""
import base64,hashlib,json,struct
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
CONTRACT=json.loads((ROOT/'deploy/v3/outbox/files/contract.json').read_text())
CONFIG=json.loads((ROOT/'deploy/v3/outbox/files/connector.json').read_text())
ENV_CONFIG={'config.providers':'env','config.providers.env.class':'org.apache.kafka.common.config.provider.EnvVarConfigProvider','config.providers.env.param.allowlist.pattern':'^(V3_CDC_PASSWORD|V3_KAFKA_PASSWORD|V3_KAFKA_CA_PEM)$','topic.creation.enable':False,'producer.max.request.size':262144}
ENV_REFS=[{'name':'V3_CDC_PASSWORD','valueFrom':{'secretKeyRef':{'name':'mysql-cdc','key':'password'}}},{'name':'V3_KAFKA_PASSWORD','valueFrom':{'secretKeyRef':{'name':'v3-connect-outbox','key':'password'}}},{'name':'V3_KAFKA_CA_PEM','valueFrom':{'secretKeyRef':{'name':'yeodam-v3-kafka-cluster-ca-cert','key':'ca.crt'}}}]
def require(value,message):
 if not value:raise ValueError(message)
def public_jks(encoded):
 try:data=base64.b64decode(encoded,validate=True)
 except Exception:raise ValueError('Invalid public truststore encoding') from None
 require(100<len(data)<12288,'Public truststore size outside bounds')
 pos=0
 def integer(n):
  nonlocal pos
  require(pos+n<=len(data)-20,'Truncated truststore');v=int.from_bytes(data[pos:pos+n],'big');pos+=n;return v
 def utf():
  nonlocal pos
  n=integer(2);require(pos+n<=len(data)-20,'Truncated truststore string');v=data[pos:pos+n].decode();pos+=n;return v
 require(integer(4)==0xFEEDFEED and integer(4)==2 and integer(4)==1,'Exactly one public JKS certificate required')
 require(integer(4)==2,'Private key entries forbidden')
 require(utf()=='v3-mysql-ca','Only the reviewed CA alias allowed');integer(8)
 require(utf()=='X.509','Only X509 certificate entries allowed');n=integer(4)
 require(n>100 and pos+n==len(data)-20 and data[pos]==0x30,'Malformed certificate entry')
 expected=hashlib.sha1('public-ca-only'.encode('utf-16-be')+b'Mighty Aphrodite'+data[:-20]).digest()
 require(data[-20:]==expected,'Public truststore integrity mismatch')
 return data
