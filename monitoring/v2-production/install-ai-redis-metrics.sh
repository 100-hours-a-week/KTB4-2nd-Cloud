#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 && -n "${AI_REDIS_METRICS_ADDRESS:-}" ]] || exit 1
image='oliver006/redis_exporter:v1.94.0@sha256:0842ef7110e62b40bbde2d5c11f38c6fbd99f7100c75e3866637c1b335a1eff7'
redis_id=$(docker inspect --format '{{.Id}}' yeodam-ai-redis)
redis_started=$(docker inspect --format '{{.State.StartedAt}}' yeodam-ai-redis)
install -d -m 0700 /etc/yeodam/ai-redis-metrics
python3 - <<'PY'
import hashlib,ipaddress,json,os,pathlib,secrets,socket,tempfile
address=os.environ['AI_REDIS_METRICS_ADDRESS']
assert ipaddress.ip_address(address) in ipaddress.ip_network('10.20.0.0/16')
base=pathlib.Path('/etc/yeodam/ai-redis-metrics');password_file=base/'passwords.json'
uri='redis://yeodam_metrics@'+address+':6379'
password=json.loads(password_file.read_text())[uri] if password_file.exists() else secrets.token_hex(32)
assert len(password)==64 and all(c in '0123456789abcdef' for c in password)
class Redis:
 def __init__(self):
  self.sock=socket.create_connection((address,6379),timeout=5);self.reader=self.sock.makefile('rb')
 def command(self,*args):
  parts=[str(a).encode() for a in args]
  self.sock.sendall(b'*'+str(len(parts)).encode()+b'\r\n'+b''.join(b'$'+str(len(a)).encode()+b'\r\n'+a+b'\r\n' for a in parts))
  return self.read()
 def read(self):
  head=self.reader.readline();kind=head[:1];value=head[1:-2]
  if kind==b'+':return value.decode()
  if kind==b'-':raise RuntimeError('Redis rejected a provisioning command')
  if kind==b':':return int(value)
  if kind==b'$':
   n=int(value)
   if n<0:return None
   value=self.reader.read(n);assert self.reader.read(2)==b'\r\n';return value.decode()
  if kind==b'*':return [self.read() for _ in range(int(value))]
  raise RuntimeError('Unexpected Redis response')
r=Redis();assert r.command('AUTH',pathlib.Path('/etc/yeodam/ai-redis/password').read_text().strip())=='OK'
default_before=r.command('ACL','GETUSER','default')
acl_path=pathlib.Path('/etc/yeodam/ai-redis/users.acl');original=acl_path.read_text();inode=acl_path.stat().st_ino
# No key patterns and no read/write/SCAN/EVAL/CONFIG privileges. Slowlog reads are numeric-only in exporter output.
rules=['on','#'+hashlib.sha256(password.encode()).hexdigest(),'resetkeys','resetchannels','-@all',
       '+ping','+info','+select','+client|setname','+client|setinfo','+slowlog|len','+slowlog|get','+command|info']
assert r.command('ACL','SETUSER','yeodam_metrics','reset',*rules)=='OK'
lines=[line for line in original.splitlines() if not line.startswith('user yeodam_metrics ')]
lines.append('user yeodam_metrics '+' '.join(rules))
# Keep inode: Redis already has this individual ACL file bind-mounted read-only.
with acl_path.open('r+') as f:
 f.seek(0);f.write('\n'.join(lines)+'\n');f.flush();f.truncate();os.fsync(f.fileno())
assert acl_path.stat().st_ino==inode
assert r.command('ACL','GETUSER','default')==default_before
with tempfile.NamedTemporaryFile(mode='w',dir=base,delete=False) as f:
 temporary=pathlib.Path(f.name);json.dump({uri:password},f);f.write('\n');os.fchmod(f.fileno(),0o400);os.fchown(f.fileno(),65534,65534)
os.replace(temporary,password_file)
assert r.command('PING')=='PONG'
print('Monitoring ACL ready; application default ACL unchanged; password file 0400')
PY
docker pull "$image"
cat > /etc/systemd/system/yeodam-ai-redis-metrics.service <<UNIT
[Unit]
Description=Yeodam AI Redis server metrics exporter
Requires=docker.service
After=docker.service network-online.target yeodam-ai-redis.service
Wants=network-online.target
[Service]
Restart=on-failure
RestartSec=5
ExecStartPre=-/usr/bin/docker rm yeodam-ai-redis-metrics
ExecStart=/usr/bin/docker run --name yeodam-ai-redis-metrics --network host --user 65534:65534 --read-only --cap-drop ALL --security-opt no-new-privileges --memory 128m --cpus 0.25 --log-driver journald -v /etc/yeodam/ai-redis-metrics/passwords.json:/run/secrets/passwords.json:ro $image --redis.addr=redis://$AI_REDIS_METRICS_ADDRESS:6379 --redis.user=yeodam_metrics --redis.password-file=/run/secrets/passwords.json --web.listen-address=$AI_REDIS_METRICS_ADDRESS:9121 --config-command=- --exclude-latency-histogram-metrics --disable-scrape-endpoint --redis-only-metrics --connection-timeout=5s
ExecStop=/usr/bin/docker stop -t 10 yeodam-ai-redis-metrics
TimeoutStopSec=20
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable yeodam-ai-redis-metrics
systemctl restart yeodam-ai-redis-metrics
for attempt in $(seq 1 18); do
 if curl --noproxy '*' -fsS --max-time 10 "http://$AI_REDIS_METRICS_ADDRESS:9121/metrics" | grep '^redis_up 1$' >/dev/null; then
  [[ "$redis_id" == "$(docker inspect --format '{{.Id}}' yeodam-ai-redis)" ]]
  [[ "$redis_started" == "$(docker inspect --format '{{.State.StartedAt}}' yeodam-ai-redis)" ]]
  echo 'AI Redis container unchanged; redis_up=1'
  exit 0
 fi
 sleep 3
done
echo 'AI Redis metrics did not become ready' >&2
exit 1
