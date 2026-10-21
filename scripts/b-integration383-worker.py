#!/usr/bin/env python3
"""Isolate migration checkpoint cadence; leave shared Connect/Outbox untouched."""
import http.client,json,socket,time
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen
NAME='yeodam-b383-connect';ORIGINAL='yeodam-v2-staging-kafka-connect-1'
class Connection(http.client.HTTPConnection):
 def connect(self):
  self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.connect('/var/run/docker.sock')
def docker(method,path,data=None):
 c=Connection('localhost',timeout=30);c.request(method,path,body=None if data is None else json.dumps(data).encode(),headers={'Content-Type':'application/json'});r=c.getresponse();b=r.read();c.close()
 if r.status not in (200,201,204):raise RuntimeError('Owned Worker Docker HTTP '+str(r.status))
 return json.loads(b) if b else {}
def start():
 available=int(next(line.split()[1] for line in open('/proc/meminfo') if line.startswith('MemAvailable:')))
 if available<1280*1024:raise RuntimeError('Insufficient memory for bounded owned Worker')
 old=docker('GET','/containers/'+ORIGINAL+'/json')
 if not old['State']['Running']:raise RuntimeError('Shared Connect not running')
 env={'BOOTSTRAP_SERVERS':'10.30.10.95:9092','GROUP_ID':'yeodam-b383-connect',
 'CONFIG_STORAGE_TOPIC':'staging.yeodam.b383.connect.configs','OFFSET_STORAGE_TOPIC':'staging.yeodam.b383.connect.offsets','STATUS_STORAGE_TOPIC':'staging.yeodam.b383.connect.status',
 'CONNECT_CONFIG_STORAGE_REPLICATION_FACTOR':'1','CONNECT_OFFSET_STORAGE_REPLICATION_FACTOR':'1','CONNECT_STATUS_STORAGE_REPLICATION_FACTOR':'1','CONNECT_OFFSET_STORAGE_PARTITIONS':'3','CONNECT_STATUS_STORAGE_PARTITIONS':'1',
 'OFFSET_FLUSH_INTERVAL_MS':'1000','REST_PORT':'18083','REST_HOST_NAME':'127.0.0.1','CONNECT_LISTENERS':'http://127.0.0.1:18083','ADVERTISED_HOST_NAME':'127.0.0.1','ADVERTISED_PORT':'18083',
 'CONNECT_CONNECTOR_CLIENT_CONFIG_OVERRIDE_POLICY':'All','CONNECT_CONFIG_PROVIDERS':'file','CONNECT_CONFIG_PROVIDERS_FILE_CLASS':'org.apache.kafka.common.config.provider.FileConfigProvider','HEAP_OPTS':'-Xms128m -Xmx512m','KAFKA_HEAP_OPTS':'-Xms128m -Xmx512m','JAVA_TOOL_OPTIONS':'-XX:ActiveProcessorCount=1'}
 mounts=[m for m in old['Mounts'] if m['Destination'] in ('/run/yeodam-cdc','/kafka/connect/debezium-connector-mysql/cloud-cdc-libs')]
 if len(mounts)!=2:raise RuntimeError('Expected existing read-only credential/plugin mounts')
 plugin_dir=Path('/opt/yeodam-b383/cdc-libs')
 if not (plugin_dir/'yeodam-auth368-smt.jar').is_file():raise RuntimeError('Owned recovery SMT library not prepared')
 for m in mounts:
  if m['Destination'].endswith('/cloud-cdc-libs'):m['Source']=str(plugin_dir)
 payload={'Image':old['Config']['Image'],'Env':[k+'='+v for k,v in env.items()],'Cmd':old['Config']['Cmd'],
 'Labels':{'yeodam.issue':'383','yeodam.purpose':'migration-only-connect'},'HostConfig':{'NetworkMode':'host','RestartPolicy':{'Name':'no'},'Memory':896*1024*1024,'NanoCpus':1000000000,'Binds':[m['Source']+':'+m['Destination']+':ro' for m in mounts]}}
 docker('POST','/containers/create?name='+NAME,payload);docker('POST','/containers/'+NAME+'/start')
 end=time.monotonic()+180
 while time.monotonic()<end:
  try:
   with urlopen('http://127.0.0.1:18083/connector-plugins',timeout=2) as r:plugins=json.load(r)
   available={x['class'] for x in plugins}
   if {'io.debezium.connector.mysql.MySqlConnector','io.debezium.connector.jdbc.JdbcSinkConnector'}<=available:print(json.dumps({'worker':NAME,'ready':True,'offsetFlushMs':1000,'cpuLimit':1,'memoryLimitMiB':896}));return
  except Exception:pass
  time.sleep(1)
 raise RuntimeError('Owned Worker did not become ready; shared Worker unchanged')
if __name__=='__main__':start()
