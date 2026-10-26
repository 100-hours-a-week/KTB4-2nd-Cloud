#!/usr/bin/env python3
"""V1 staging only. Bounded capacity/TCP trials with independent timed rollback."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from uuid import uuid4
from nginx_tcp_guard import capacity, nft_policy, TABLE

BASE = Path('/var/lib/yeodam/nginx-tcp436')
MAIN = Path('/etc/nginx/nginx.conf')
SITE = Path('/etc/nginx/sites-enabled/yeodam-v1-staging')
UNIT = 'yeodam-tcp436-lease'
SERVICE = 'yeodam-nginx-tcp436'


def install_metrics():
    script = Path(__file__).with_name('nginx-tcp-metrics.py')
    if not script.is_file():
        raise ValueError('Deploy the reviewed metrics script beside this installer')
    name = 'yeodam-nginx-tcp436-metrics'
    Path('/etc/systemd/system/'+name+'.service').write_text(
        '[Unit]\nDescription=Read V1 TCP guard counters\n[Service]\nType=oneshot\nNice=10\nMemoryMax=64M\nCPUQuota=10%\nExecStart=/usr/bin/python3 '+str(script)+'\n')
    Path('/etc/systemd/system/'+name+'.timer').write_text(
        '[Unit]\nDescription=Observe V1 TCP guard counters\n[Timer]\nOnBootSec=20s\nOnUnitActiveSec=15s\n[Install]\nWantedBy=timers.target\n')
    run(['systemctl','daemon-reload']);run(['systemctl','enable','--now',name+'.timer'])
    run(['systemctl','start',name+'.service'])


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=20, **kwargs).stdout


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cancel_lease():
    subprocess.run(['systemctl', 'stop', UNIT+'.timer'], capture_output=True)
    state = BASE/'lease.json'
    if state.exists():
        old = json.loads(state.read_text())
        subprocess.run(['systemctl', 'stop', old['unit']+'.timer'], capture_output=True)
        state.unlink()


def lease(seconds):
    cancel_lease()
    token = str(uuid4()); unit = UNIT+'-'+token
    (BASE/'lease.json').write_text(json.dumps({'unit': unit, 'token': token}))
    run(['systemd-run', '--quiet', '--collect', '--unit='+unit, '--on-active='+str(seconds)+'s',
         'python3', str(Path(__file__).resolve()), 'rollback', '--lease-id', token,
         '--expected-site-sha256', sha(SITE)])


def remove_table():
    if subprocess.run(['nft', 'list', 'table', 'inet', TABLE], capture_output=True).returncode == 0:
        run(['nft', 'delete', 'table', 'inet', TABLE])


def rollback():
    subprocess.run(['systemctl', 'disable', '--now', SERVICE], capture_output=True)
    remove_table()
    policy = BASE/'policy.json'
    if policy.exists():
        old = json.loads(policy.read_text()); old['active'] = False
        policy.write_text(json.dumps(old, indent=2))
    state = BASE/'capacity.json'
    if state.exists():
        meta = json.loads(state.read_text())
        if sha(MAIN) != meta['after']:
            raise ValueError('Main configuration changed independently; inspect before restore')
        old = Path(meta['backup']).read_bytes()
        if hashlib.sha256(old).hexdigest() != meta['before']:
            raise ValueError('Backup checksum differs')
        MAIN.write_bytes(old)
        run(['nginx', '-t']); run(['systemctl', 'reload', 'nginx'])
        state.rename(BASE/('capacity-restored-'+str(time.time_ns())+'.json'))
    print(json.dumps({'rolledBack': True, 'siteSha256': sha(SITE), 'mainSha256': sha(MAIN)}))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['capacity', 'policy', 'rollback', 'commit', 'status', 'metrics'])
    p.add_argument('--expected-site-sha256', required=True)
    p.add_argument('--expected-main-sha256')
    p.add_argument('--connections', type=int, choices=[768,1536,2048])
    p.add_argument('--per-source', type=int, default=0)
    p.add_argument('--rate', type=int, default=0)
    p.add_argument('--burst', type=int, default=40)
    p.add_argument('--enforce', action='store_true')
    p.add_argument('--lease-seconds', type=int, default=1200)
    p.add_argument('--lease-id')
    a = p.parse_args()
    if os.geteuid() != 0 or Path('/var/lib/cloud/data/instance-id').read_text().strip() != 'i-05d320e3a87200d5a':
        p.error('Root on exact V1 staging App only')
    if a.mode not in ['rollback','status'] and (sha(SITE) != a.expected_site_sha256 or 'keepalive_timeout 15s;' not in SITE.read_text()):
        p.error('Site changed or measured Keepalive missing')
    if not 120 <= a.lease_seconds <= 3600:
        p.error('Trial lease 120..3600 seconds required')
    os.umask(0o077); BASE.mkdir(parents=True, exist_ok=True)
    if a.mode not in ['rollback','status'] and len(os.sched_getaffinity(0)) != 2:
        p.error('Measured two-vCPU staging App only')
    if a.mode == 'metrics':
        install_metrics(); print(json.dumps({'metricsInstalled':True})); return
    if a.mode == 'rollback':
        if a.lease_id and (not (BASE/'lease.json').exists() or json.loads((BASE/'lease.json').read_text())['token'] != a.lease_id):
            print(json.dumps({'staleLeaseIgnored': True})); return
        cancel_lease(); rollback(); return
    if a.mode == 'status':
        print(json.dumps({'siteSha256':sha(SITE),'mainSha256':sha(MAIN),
            'capacity':json.loads((BASE/'capacity.json').read_text()) if (BASE/'capacity.json').exists() else None,
            'policy':json.loads((BASE/'policy.json').read_text()) if (BASE/'policy.json').exists() else None,
            'nft':run(['nft','-j','list','tables'])})); return
    if a.mode == 'capacity':
        if not a.connections or sha(MAIN) != a.expected_main_sha256:
            p.error('Explicit capacity and matching main SHA required')
        previous = MAIN.read_bytes(); updated = capacity(previous.decode(), a.connections).encode()
        path = BASE/'capacity.json'
        if path.exists():
            meta = json.loads(path.read_text())
            if meta['after'] != sha(MAIN):
                p.error('Unexpected main mutation')
        else:
            descriptor, name = tempfile.mkstemp(dir=BASE, prefix='nginx-before-')
            os.close(descriptor); backup = Path(name)
            backup.write_bytes(previous)
            meta = {'backup':str(backup),'before':sha(MAIN)}
        # Arm recovery BEFORE changing capacity, independent of client/session failures.
        meta.update(after=hashlib.sha256(updated).hexdigest(), connectionsPerWorker=a.connections,
                    workerCount=2, fdLimitPerWorker=4096, siteSha256=sha(SITE))
        path.write_text(json.dumps(meta,indent=2)); lease(a.lease_seconds)
        try:
            MAIN.write_bytes(updated); run(['nginx','-t']); run(['systemctl','reload','nginx'])
        except Exception:
            MAIN.write_bytes(previous); run(['nginx','-t']); run(['systemctl','reload','nginx'])
            meta['after']=hashlib.sha256(previous).hexdigest();path.write_text(json.dumps(meta));raise
        print(json.dumps({'capacityApplied':meta,'trialLeaseSeconds':a.lease_seconds})); return
    if a.mode == 'policy':
        config = nft_policy(a.per_source,a.rate,a.burst,a.enforce)
        exists = subprocess.run(['nft','list','table','inet',TABLE],capture_output=True).returncode == 0
        batch = ('delete table inet '+TABLE+'\n' if exists else '')+config
        target=BASE/'candidate.nft';target.write_text(batch)
        run(['nft','-c','-f',str(target)])
        lease(a.lease_seconds)
        run(['nft','-f',str(target)])
        (BASE/'policy.nft').write_text(config)
        meta={'perSource':a.per_source,'newSynRate':a.rate,'burst':a.burst,'enforced':a.enforce,'active':True,
              'policySha256':hashlib.sha256(config.encode()).hexdigest(),'ports':[80,443],
              'counterMeaning':'Conntrack flows and initial SYN packets, not HTTP request counts'}
        (BASE/'policy.json').write_text(json.dumps(meta,indent=2))
        print(json.dumps({'policyApplied':meta,'trialLeaseSeconds':a.lease_seconds})); return
    if a.mode == 'commit':
        meta=json.loads((BASE/'policy.json').read_text())
        if not meta['enforced'] or not meta.get('active') or not a.expected_main_sha256 or sha(MAIN)!=a.expected_main_sha256:
            p.error('Only reviewed enforced policy and explicit current main SHA can persist')
        config=(BASE/'policy.nft').read_text()
        if hashlib.sha256(config.encode()).hexdigest()!=meta['policySha256']:
            p.error('Policy checksum differs')
        service=Path('/etc/systemd/system/'+SERVICE+'.service')
        service.write_text('[Unit]\nDescription=V1 staging source TCP guard\nAfter=network-pre.target ufw.service docker.service\nBefore=nginx.service\n\n[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/sbin/nft -f '+str(BASE/'policy.nft')+'\nExecStop=-/usr/sbin/nft delete table inet '+TABLE+'\n\n[Install]\nWantedBy=multi-user.target\n')
        run(['systemctl','daemon-reload'])
        subprocess.run(['systemctl','stop',SERVICE],capture_output=True)
        remove_table();run(['systemctl','enable',SERVICE]);run(['systemctl','start',SERVICE])
        install_metrics()
        cancel_lease()
        print(json.dumps({'persisted':True,'mainSha256':sha(MAIN),'policy':meta}))


if __name__=='__main__':
    main()
