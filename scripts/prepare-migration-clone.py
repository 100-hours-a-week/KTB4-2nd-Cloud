#!/usr/bin/env python3
"""Restore the reviewed baseline on a quarantined #444 clone, never original hosts."""
import argparse
import gzip
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
import tempfile
import urllib.request

ORIGINALS = {'i-05d320e3a87200d5a', 'i-05535ae6ff812972c'}
SCHEMA = 'yeodam_v1_source'
BASELINE = 's3://yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2/migration/rehearsal-reset/3502031b-0040-4333-9a8c-9ce63d9a2ce8/b391-target.sql.gz'
BASELINE_SHA = 'ab1a0935f6335f0385f5df2cfb2eb4c75c9fce4f05a742d9e81bed3bfe2cfae6'
IMAGE = 'ghcr.io/100-hours-a-week/yeodam-backend@sha256:d39fd16fc92e77d9c51be9056a2d0c79e355fdf0ad81e38a8d463ab554fd2010'
ROOT = Path('/var/lib/yeodam/migration444')


def run(args, *, data=None, env=None):
    p = subprocess.run(args, input=data, capture_output=True, text=True, env=env)
    if p.returncode:
        # A DB/registry error can contain credentials or row values. Keep it local.
        ROOT.mkdir(parents=True, exist_ok=True)
        (ROOT / 'last-error.private').write_text(p.stderr)
        raise RuntimeError(args[0] + ' failed; private stderr retained on clone')
    return p.stdout.strip()


def identity():
    req = urllib.request.Request('http://169.254.169.254/latest/api/token', method='PUT',
                                 headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'})
    with urllib.request.urlopen(req, timeout=3) as r:
        token = r.read().decode()
    req = urllib.request.Request('http://169.254.169.254/latest/dynamic/instance-identity/document',
                                 headers={'X-aws-ec2-metadata-token': token})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.load(r)


def guard(role, expected_instance, expected_volume, document, marker, mounted_serial):
    if (expected_instance in ORIGINALS or document['instanceId'] in ORIGINALS
            or document['instanceId'] != expected_instance
            or document['accountId'] != '483175530259'
            or document['region'] != 'ap-northeast-2'):
        raise ValueError('Not an approved clone instance')
    if marker.get('issue') != 444 or marker.get('role') != role:
        raise ValueError('Missing clone quarantine identity')
    if mounted_serial.replace('-', '').lower() != expected_volume.replace('-', '').lower():
        raise ValueError('Clone data volume is not mounted at its expected path')


def mysql_args(role, database=False):
    if role == 'source':
        args = ['mysql', '--protocol=socket', '-uroot', '--default-character-set=utf8mb4']
    else:
        args = ['docker', 'exec', '-i', 'yeodam-v2-mysql', 'sh', '-ec',
                'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot --default-character-set=utf8mb4 "$@"', 'sh']
    if database:
        args.append(SCHEMA)
    else:
        args += ['--batch', '--skip-column-names']
    return args


def query(role, sql):
    return run(mysql_args(role), data=sql)


def start_database(role):
    if role == 'target':
        # Copied bootstrap references the original volume/Secret. The snapshot
        # filesystem is already mounted by its copied UUID; do not run that hook.
        path = Path('/etc/systemd/system/yeodam-mysql.service.d/444-clone.conf')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[Service]\nExecStartPre=\nExecStartPre=/usr/bin/mountpoint -q /srv/yeodam/mysql\nExecStartPre=/usr/bin/test -s /etc/yeodam/mysql-root-password\nExecStartPre=-/usr/bin/docker rm -f yeodam-v2-mysql\n')
        run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'start', 'mysql' if role == 'source' else 'yeodam-mysql'])
    deadline = time.monotonic() + 180
    while True:
        try:
            if query(role, 'SELECT 1;') == '1':
                return
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
        time.sleep(3)


def restore(role):
    receipt = ROOT / 'baseline-restored.json'
    if receipt.exists():
        raise ValueError('Baseline already restored; inspect receipt, do not reset again')
    archive = ROOT / 'baseline.sql.gz'
    run(['aws', 's3', 'cp', BASELINE, str(archive), '--region', 'ap-northeast-2', '--only-show-errors'])
    digest = hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest()
    if digest != BASELINE_SHA:
        raise ValueError('Baseline archive SHA mismatch; DB unchanged')
    start_database(role)
    if query(role, 'SHOW REPLICA STATUS;'):
        query(role, 'STOP REPLICA;')
    clients = query(role, "SELECT COUNT(*) FROM information_schema.processlist WHERE USER NOT IN ('root','event_scheduler','system user') AND ID!=CONNECTION_ID();")
    if clients != '0':
        raise ValueError('Unexpected DB clients; no schema reset performed')
    query(role, 'SET PERSIST super_read_only=OFF; SET PERSIST read_only=OFF;')
    query(role, 'DROP DATABASE IF EXISTS ' + SCHEMA + '; CREATE DATABASE ' + SCHEMA + ' CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;')
    with gzip.open(archive, 'rb') as source, (ROOT / 'import.stderr.private').open('wb') as error:
        p = subprocess.Popen(mysql_args(role, database=True), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=error)
        try:
            shutil.copyfileobj(source, p.stdin, length=1024 * 1024)
            p.stdin.close()
        except BaseException:
            p.kill()
            p.wait()
            raise
        if p.wait():
            raise RuntimeError('Baseline import failed; preserve private stderr')
    if role == 'target':
        query(role, 'SET PERSIST read_only=ON; SET PERSIST super_read_only=ON;')
    result = verify(role)
    result['baselineSha256'] = digest
    receipt.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)


def verify(role):
    values = query(role, f"SELECT (SELECT COUNT(*) FROM {SCHEMA}.trips),(SELECT COUNT(*) FROM {SCHEMA}.trip_regions),(SELECT COUNT(*) FROM {SCHEMA}.migration_trip_request_result),(SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='{SCHEMA}' AND table_type='BASE TABLE');")
    if values.split('\t') != ['2021037', '2021037', '219511', '33']:
        raise ValueError('Baseline row/table counts differ')
    settings = query(role, 'SELECT @@version,@@max_connections,@@innodb_buffer_pool_size,@@innodb_flush_log_at_trx_commit,@@sync_binlog,@@binlog_format,@@server_id,@@gtid_mode,@@innodb_redo_log_capacity;').split('\t')
    if settings != ['9.7.2', '151', '134217728', '1', '1', 'ROW', '1' if role == 'source' else '2', 'ON', '104857600']:
        raise ValueError('MySQL performance settings differ')
    tables = query(role, f"SELECT table_name FROM information_schema.tables WHERE table_schema='{SCHEMA}' AND table_type='BASE TABLE' ORDER BY table_name;").splitlines()
    sums = query(role, 'CHECKSUM TABLE ' + ','.join(SCHEMA + '.`' + n + '`' for n in tables) + ';')
    increments = query(role, f"SELECT table_name,COALESCE(auto_increment,0) FROM information_schema.tables WHERE table_schema='{SCHEMA}' AND table_type='BASE TABLE' ORDER BY table_name;")
    indexes = query(role, "SHOW INDEX FROM " + SCHEMA + '.trips;')
    # Cardinality/statistics can vary after restoration. Keep structure separately.
    structure = '\n'.join('\t'.join(row.split('\t')[i] for i in [1, 2, 3, 4, 5, 7, 10]) for row in indexes.splitlines())
    return {'role': role, 'trips': 2021037, 'regions': 2021037, 'journal': 219511, 'tables': 33,
            'checksumsSha256': hashlib.sha256(sums.encode()).hexdigest(),
            'autoIncrementSha256': hashlib.sha256(increments.encode()).hexdigest(),
            'tripIndexStructureSha256': hashlib.sha256(structure.encode()).hexdigest(),
            'settings': settings}


def prepare_nginx():
    # Only the guarded clone reaches this function. Never change the original
    # Nginx defense work; restore the pre-defense files copied in its AMI.
    candidates = [
        (Path('/var/lib/yeodam/nginx-tcp436/nginx-before-aj14_46e'),
         Path('/etc/nginx/nginx.conf'),
         '48c6a4ec1e1fd28ccf968490f07e34a1d7f755793b2108a3ed8670b1ee2a0aa2'),
        (Path('/var/lib/yeodam/b391/3502031b-0040-4333-9a8c-9ce63d9a2ce8/nginx.before'),
         Path('/etc/nginx/sites-available/yeodam-v1-staging'),
         '5f4d61ea8924e1245595867781925d2e208c1fab12f4357f74968b33d7d283d5'),
    ]
    for old, _, expected in candidates:
        if hashlib.sha256(old.read_bytes()).hexdigest() != expected:
            raise ValueError('Historical Nginx backup differs; no config changed')
    saved = ROOT / 'nginx-copied-config'
    if saved.exists():
        raise ValueError('Nginx preparation already attempted; inspect saved files')
    saved.mkdir(mode=0o700)
    for directory in ['conf.d', 'sites-enabled']:
        shutil.copytree('/etc/nginx/' + directory, saved / directory, symlinks=True)
    shutil.copy2('/etc/nginx/nginx.conf', saved / 'nginx.conf')
    for old, dest, _ in candidates:
        shutil.copy2(old, dest)
    for directory in ['conf.d', 'sites-enabled']:
        for entry in Path('/etc/nginx', directory).iterdir():
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            else:
                raise ValueError('Unexpected Nginx include directory; preserve stopped clone')
    Path('/etc/nginx/sites-enabled/yeodam-v1-staging').symlink_to('/etc/nginx/sites-available/yeodam-v1-staging')
    run(['nginx', '-t'])
    # nginx -T appends a trailing newline. run() trims it, so preserve the exact
    # bytes for comparison with the successful-run evidence.
    result = subprocess.run(['nginx', '-T'], capture_output=True, check=True).stdout
    digest = hashlib.sha256(result).hexdigest()
    if digest != '3c509c7e0ca301ae9258f79a73d417c03bf0d7de6174d8a3c2d9004b2ccedffb':
        raise ValueError('Historical aggregate Nginx SHA differs; leave Nginx stopped')
    print(json.dumps({'phase': 'historical_nginx_prepared', 'nginxSha256': digest, 'nginxStarted': False}))


class DockerConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect('/var/run/docker.sock')


def docker(method, path, payload=None):
    c = DockerConnection('localhost', timeout=90)
    c.request(method, path, body=None if payload is None else json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    response = c.getresponse()
    body = response.read()
    if response.status not in [200, 201, 204]:
        raise RuntimeError('Clone Docker action failed, HTTP ' + str(response.status))
    return json.loads(body) if body else {}


def ensure_image():
    # Use the same scoped registry secret only on the clone. Do not persist
    # credentials in Docker's durable config or print them in SSM output.
    result = subprocess.run(['docker', 'image', 'inspect', IMAGE], capture_output=True)
    if result.returncode == 0:
        return
    with tempfile.TemporaryDirectory(prefix='migration444-registry-', dir='/run') as directory:
        env = os.environ.copy()
        env['DOCKER_CONFIG'] = directory
        response = json.loads(run(['aws', '--region', 'ap-northeast-2', 'secretsmanager',
                                   'get-secret-value', '--secret-id',
                                   'yeodam/v2/staging/ghcr-credentials', '--output', 'json']))
        auth = json.loads(response['SecretString'])
        run(['docker', 'login', 'ghcr.io', '--username', auth['username'], '--password-stdin'],
            data=auth['password'], env=env)
        run(['docker', 'pull', '--platform', 'linux/amd64', IMAGE], env=env)


def source_app(target_address, source_address):
    address = ipaddress.ip_address(target_address)
    if address not in ipaddress.ip_network('10.30.10.0/24') or target_address == '10.30.10.28':
        raise ValueError('Expected the new clone Target address')
    if not (ROOT / 'baseline-restored.json').exists():
        raise ValueError('Restore and verify baseline before starting BE')
    ensure_image()
    base = docker('GET', '/containers/yeodam-app-backend-1/json')
    if base['State']['Running']:
        raise ValueError('Copied original container must remain stopped')
    env = dict(v.split('=', 1) for v in base['Config']['Env'] if '=' in v)
    if env.get('MYSQL_URL') != 'jdbc:mysql://proxysql:6033/yeodam_v1_source':
        raise ValueError('Copied app DB route differs from the successful ProxySQL route')
    # The preparation phase starts no Kafka consumers and no auth bridge. Full
    # #391 run wiring is enabled in the separate measurement issue.
    env.update({'SPRING_FLYWAY_ENABLED': 'false', 'SPRING_JPA_OPEN_IN_VIEW': 'false',
                'MIGRATION_TRIP_MODE_FILE': '', 'MIGRATION_TRIP_CONSUMER_ENABLED': 'false',
                'MIGRATION_AUTH_ENABLED': 'false', 'MIGRATION_AUTH_CONSUMER_ENABLED': 'false',
                'MIGRATION_AUTH_RESULTS_ENABLED': 'false', 'SPRING_KAFKA_LISTENER_AUTO_STARTUP': 'false',
                'MIGRATION_SERVICE_SCHEDULERS_ENABLED': 'false',
                'JAVA_TOOL_OPTIONS': '-Xms128m -Xmx512m -XX:ActiveProcessorCount=2'})
    # Preserve the same ProxySQL hop; replace network identities only.
    docker('POST', '/containers/yeodam-v2-staging-proxysql-proxysql-1/start')
    proxy_env = os.environ.copy()
    proxy_env['MYSQL_PWD'] = Path('/etc/yeodam/v2-staging-proxysql/admin-password').read_text().strip()
    sql = (f"UPDATE mysql_servers SET hostname='{source_address}' WHERE hostgroup_id=10;"
           f"UPDATE mysql_servers SET hostname='{target_address}' WHERE hostgroup_id=20;"
           "LOAD MYSQL SERVERS TO RUNTIME; SAVE MYSQL SERVERS TO DISK;"
           "UPDATE mysql_users SET default_hostgroup=10; LOAD MYSQL USERS TO RUNTIME; SAVE MYSQL USERS TO DISK;")
    end = time.monotonic() + 30
    while True:
        try:
            run(['mysql', '--protocol=tcp', '-h127.0.0.1', '-P16032', '-uradmin', '--batch'], data=sql, env=proxy_env)
            break
        except RuntimeError:
            if time.monotonic() >= end:
                raise
            time.sleep(1)
    del proxy_env
    payload = {'Image': IMAGE, 'Env': [k + '=' + v for k, v in env.items()],
               'HostConfig': {'NetworkMode': 'yeodam-app-network', 'RestartPolicy': {'Name': 'no'},
                              'PortBindings': {'8080/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8080'}]}},
               'ExposedPorts': {'8080/tcp': {}}, 'Labels': {'yeodam.issue': '444', 'yeodam.phase': 'preparation'}}
    response = docker('POST', '/containers/create?name=yeodam-migration-perf-backend', payload)
    docker('POST', '/containers/' + response['Id'] + '/start')
    print(json.dumps({'phase': 'preparation_be_started', 'image': IMAGE, 'consumersEnabled': False}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['restore-data', 'verify', 'prepare-source-app', 'prepare-nginx'])
    parser.add_argument('--role', choices=['source', 'target'], required=True)
    parser.add_argument('--instance-id', required=True)
    parser.add_argument('--data-volume-id', required=True)
    parser.add_argument('--target-address')
    args = parser.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0 or not re.fullmatch(r'i-[0-9a-f]{17}', args.instance_id) or not re.fullmatch(r'vol-[0-9a-f]{17}', args.data_volume_id):
        raise ValueError('Root and exact clone instance/volume IDs required')
    mount = '/var/lib/mysql' if args.role == 'source' else '/srv/yeodam/mysql'
    if subprocess.run(['mountpoint', '-q', mount]).returncode:
        raise ValueError('Dedicated clone data EBS is not mounted')
    device = run(['findmnt', '-n', '-o', 'SOURCE', mount])
    serial = run(['lsblk', '-dn', '-o', 'SERIAL', device])
    document = identity()
    guard(args.role, args.instance_id, args.data_volume_id, document, json.loads((ROOT / 'identity.json').read_text()), serial)
    if args.action == 'restore-data':
        restore(args.role)
    elif args.action == 'verify':
        print(json.dumps(verify(args.role)), flush=True)
    elif args.action == 'prepare-nginx':
        if args.role != 'source':
            raise ValueError('Nginx belongs to the Source clone')
        prepare_nginx()
    else:
        if args.role != 'source':
            raise ValueError('BE runs on Source, not Target')
        if not args.target_address:
            raise ValueError('New Target clone address required')
        source_app(args.target_address, document['privateIp'])


if __name__ == '__main__':
    main()
