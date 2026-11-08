"""Non-secret deployment input checks shared by CLI and CI."""
import re
from urllib.parse import urlsplit
BUCKET='yeodam-v3-foundation-app-data-483175530259-ap-northeast-2'
ROLES=['auth-api','trip-api','photo-api','photo-converter','trip-result-worker']
KEYS={'AI_SERVER_BASE_URL','AI_EC2_INSTANCE_ID','JWT_ISSUER','FRONTEND_ORIGIN','FRONTEND_OAUTH_CALLBACK_URI','KAKAO_REDIRECT_URI'}
def require(ok,message):
 if not ok:raise ValueError(message)
def prepare(stage,image,release,settings,revision):
 require(stage in ['auth','all'] and re.fullmatch(r'ghcr.io/100-hours-a-week/[^\s@]+@sha256:[a-f0-9]{64}',image),'Immutable BE image required')
 require(re.fullmatch('[a-f0-9]{40}',release),'Actual BE source commit required')
 require(re.fullmatch('[a-f0-9]{64}',revision),'Configuration revision must digest public CA and Secret VersionId metadata, never values')
 require(set(settings)==KEYS|{'redisSentinelFeatureReviewed'},'Exact public endpoints and compatibility review required')
 require(type(settings['redisSentinelFeatureReviewed']) is bool,'Compatibility review must be explicit')
 require(stage=='auth' or settings['redisSentinelFeatureReviewed'],'All-role deployment requires BE Redisson Sentinel/TLS review; first auth does not prove feature compatibility')
 require(re.fullmatch(r'i-[a-f0-9]{17}',settings['AI_EC2_INSTANCE_ID']),'Explicit reviewed AI identity required; no start permission implied')
 for k in KEYS-{'AI_EC2_INSTANCE_ID'}:
  u=urlsplit(settings[k]);require(u.scheme=='https' and u.hostname and not u.username and not u.password and not u.query and not u.fragment,'Reviewed credential-free HTTPS endpoints required')
  require(u.hostname not in ['localhost','0.0.0.0'] and not u.hostname.startswith('127.'),'No loopback dependency')
 return {'images':{'backend':image},'defaults':{'configurationRevision':revision,'configMapData':{**{k:settings[k] for k in KEYS},'ATTACHMENT_S3_BUCKET':BUCKET,'RELEASE':release}},'workloads':{r:{'enabled':r=='auth-api' or stage=='all'} for r in ROLES}}
