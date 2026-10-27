#!/usr/bin/env python3
"""Read fixed TCP guard counters; no peer labels, credentials, firewall or Nginx changes."""
import json,os,re,subprocess,time
from pathlib import Path

def main():
 if os.geteuid()!=0 or Path('/var/lib/cloud/data/instance-id').read_text().strip()!='i-05d320e3a87200d5a':raise SystemExit('Exact staging App root only')
 rows=['# TYPE yeodam_nginx_tcp_guard_packets_total counter','# TYPE yeodam_nginx_tcp_guard_up gauge']
 p=subprocess.run(['nft','-j','list','counters','table','inet','yeodam_tcp436'],capture_output=True,text=True,timeout=2)
 rows.append('yeodam_nginx_tcp_guard_up '+str(int(p.returncode==0)))
 if p.returncode==0:
  for x in json.loads(p.stdout)['nftables']:
   c=x.get('counter',{});m=re.fullmatch(r'(concurrent|rate)(4|6)',c.get('name',''))
   if m:rows.append('yeodam_nginx_tcp_guard_packets_total{reason="'+m[1]+'",family="ipv'+m[2]+'"} '+str(c['packets']))
 config=Path('/etc/nginx/nginx.conf').read_text()
 slots=re.findall(r'(?m)^\s*worker_connections\s+(\d+);',config)
 if len(slots)==1:rows.append('yeodam_nginx_connection_slots '+str(int(slots[0])*len(os.sched_getaffinity(0))))
 meta=Path('/var/lib/yeodam/nginx-tcp436/policy.json')
 if meta.exists():
  d=json.loads(meta.read_text())
  for field,key in [('per_source','perSource'),('syn_rate','newSynRate'),('syn_burst','burst')]:rows.append('yeodam_nginx_tcp_guard_'+field+' '+str(d[key]))
  rows.append('yeodam_nginx_tcp_guard_enforced '+str(int(d['enforced'] and d.get('active',True) and p.returncode==0)))
 rows.append('yeodam_nginx_tcp_guard_sample_timestamp_seconds '+str(time.time()))
 root=Path('/var/lib/yeodam/mysql-metrics');root.mkdir(exist_ok=True,parents=True)
 temporary=root/'nginx-tcp436.prom.tmp';temporary.write_text('\n'.join(rows)+'\n');temporary.chmod(0o644);temporary.replace(root/'nginx-tcp436.prom')

if __name__=='__main__':main()
