"""Read-only source-host diagnostics. No auth token issue, SQL/Redis write or gate POST."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import ssl
from urllib.parse import urlsplit
from urllib.request import Request, urlopen, ProxyHandler, build_opener

from .staging import ACCOUNT, HOST, SOURCE, VPC, read_json, signature, validate
from .config import private_file


def evaluate(plan, facts, probes=None, now=None):
    validate(plan)
    now = now or datetime.now(timezone.utc)
    checks = {}
    def check(name, passed):
        checks[name] = bool(passed)
    try:
        age = (now - datetime.fromisoformat(facts['observed_at'])).total_seconds()
    except (KeyError, TypeError, ValueError):
        age = -1
    check('fresh_matching_inventory', 0 <= age <= 300 and facts.get('run_id') == plan['run_id']
          and facts.get('plan_hash') == signature(plan) and facts.get('account_id') == ACCOUNT)
    source, alb, backend = (facts.get(k, {}) for k in ('source', 'alb', 'backend'))
    check('source_instance', source == {'id': SOURCE, 'ip': plan['source']['private_ip'],
                                      'vpc': VPC, 'state': 'running'})
    check('alb_upstream_tls_name', alb.get('dns') == plan['target']['alb_dns_name']
          and alb.get('vpc') == VPC and plan['target']['tls_name'] in alb.get('certificate_names', []))
    check('backend_targets_healthy', backend.get('service') == plan['target']['service']
          and backend.get('running', 0) > 0 and backend.get('running') == backend.get('desired')
          and bool(backend.get('target_health')) and set(backend['target_health']) == {'healthy'})
    env = backend.get('env', {})
    try:
        uri = urlsplit(env.get('MYSQL_URL', '').removeprefix('jdbc:'))
        route = (uri.scheme == 'mysql' and not uri.username and not uri.password
            and uri.hostname == plan['mysql']['target_host']
            and uri.port == plan['mysql']['target_port'] and uri.path == '/' + plan['mysql']['database'])
    except ValueError:
        route = False
    check('v2_uses_migration_database', route)
    origin = 'https://' + HOST
    check('v2_public_contract', all(env.get(k) == v for k, v in {
        'JWT_ISSUER': origin, 'FRONTEND_ORIGIN': origin,
        'FRONTEND_OAUTH_CALLBACK_URI': origin + '/auth/callback',
        'KAKAO_REDIRECT_URI': origin + '/api/auth/kakao/callback'}.items()))
    check('v2_redis_contract', env.get('REDIS_HOST') == plan['redis']['host']
        and env.get('REDIS_PORT') == str(plan['redis']['port'])
        and env.get('REDIS_SSL_ENABLED') == 'true'
        and env.get('AUTH_SESSION_KEY_PREFIX') == plan['redis']['prefix'])
    check('db_test_handoff', plan['handoff']['db_tests_complete']
          and plan['handoff']['evidence'] and plan['mysql']['expected_uuid'])
    if probes is not None:
        check('executed_on_source_host', probes.get('source_host') == {
            'id': SOURCE, 'ip': plan['source']['private_ip']})
        check('v1_health', probes.get('v1_health') is True)
        check('alb_health_verified_tls', probes.get('alb_health') is True)
        db = probes.get('mysql', {})
        check('both_routes_current_writer', db.get('source_uuid') == plan['mysql']['expected_uuid']
              and db.get('target_uuid') == plan['mysql']['expected_uuid']
              and bool(db.get('source_uuid')) and db.get('both_writable') is True)
        check('expanded_schema_and_drained', db.get('schema_ready') is True
              and db.get('pending_jobs') == 0 and db.get('missing_original_sizes') == 0)
        cache = probes.get('redis', {})
        check('redis_tls_and_inactive_namespace', cache.get('master') is True and cache.get('empty') is True)
    return {'run_id': plan['run_id'], 'plan_hash': signature(plan), 'observed_at': now.isoformat(),
            'mode': 'host-read-only' if probes is not None else 'offline',
            'result': 'pass' if all(checks.values()) else 'blocked', 'checks': checks,
            'blocked': [name for name, passed in checks.items() if not passed],
            'dns_direct_alb_certificate_ready': HOST in alb.get('certificate_names', []),
            'activation_allowed': False,
            'probe_errors': {key: value['error_type'] for key, value in (probes or {}).items()
                             if isinstance(value, dict) and 'error_type' in value}}


def metadata():
    # No proxy credentials and no IAM credential endpoint. Token is not logged.
    opener = build_opener(ProxyHandler({}))
    base = 'http://169.254.169.254/latest/'
    request = Request(base + 'api/token', method='PUT', headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'})
    with opener.open(request, timeout=2) as response:
        token = response.read().decode()
    result = {}
    for key, path in [('id', 'instance-id'), ('ip', 'local-ipv4')]:
        with opener.open(Request(base + 'meta-data/' + path,
                         headers={'X-aws-ec2-metadata-token': token}), timeout=2) as response:
            result[key] = response.read().decode()
    return result


def mysql_probe(plan, secret):
    import pymysql
    c = plan['mysql']
    results = []
    for host, port in ((c['host'], c['port']), (c['target_host'], c['target_port'])):
        connection = pymysql.connect(host=host, port=port, user=c['user'], password=secret,
            database=c['database'], connect_timeout=2, read_timeout=2, write_timeout=2,
            cursorclass=pymysql.cursors.DictCursor, autocommit=False)
        try:
            with connection.cursor() as cursor:
                cursor.execute('START TRANSACTION READ ONLY')
                cursor.execute('SELECT @@server_uuid AS uuid, @@read_only AS ro, @@super_read_only AS sro')
                row = cursor.fetchone()
                cursor.execute('SELECT version FROM flyway_schema_history WHERE success=1')
                versions = {r['version'] for r in cursor.fetchall()}
                cursor.execute('''SELECT column_name FROM information_schema.columns
                    WHERE table_schema=DATABASE() AND table_name='files' ''')
                columns = {r['COLUMN_NAME'] if 'COLUMN_NAME' in r else r['column_name'] for r in cursor.fetchall()}
                row['schema_ready'] = {str(v) for v in range(1, 26)} <= versions and 'original_size_bytes' in columns
                if row['schema_ready']:
                    cursor.execute('''SELECT COUNT(*) AS n FROM trips t WHERE t.deleted_at IS NULL
                        AND t.processing_status IN ('PROCESSING','FINALIZING') AND EXISTS
                        (SELECT 1 FROM trip_attachments a WHERE a.trip_id=t.trip_id AND a.deleted_at IS NULL)''')
                    row['pending_jobs'] = cursor.fetchone()['n']
                    cursor.execute('''SELECT COUNT(*) AS n FROM files f JOIN trip_attachments a USING(file_id)
                        JOIN trips t USING(trip_id) WHERE t.processing_status='COMPLETED'
                        AND t.deleted_at IS NULL AND a.deleted_at IS NULL AND f.deleted_at IS NULL
                        AND f.original_size_bytes IS NULL''')
                    row['missing'] = cursor.fetchone()['n']
                results.append(row)
        finally:
            connection.rollback()
            connection.close()
    a, b = results
    return {'source_uuid': a['uuid'], 'target_uuid': b['uuid'],
            'both_writable': all(not r['ro'] and not r['sro'] for r in results),
            'schema_ready': all(r['schema_ready'] for r in results),
            'pending_jobs': max(r.get('pending_jobs', -1) for r in results),
            'missing_original_sizes': max(r.get('missing', -1) for r in results)}


def redis_probe(plan, secret):
    import redis
    c = plan['redis']
    with redis.Redis(host=c['host'], port=c['port'], db=c['db'], password=secret, ssl=True,
                     decode_responses=True, socket_timeout=2, socket_connect_timeout=2) as client:
        client.ping()
        master = client.info('replication')['role'] == 'master'
        cursor, keys = 0, []
        for _ in range(100):
            cursor, keys = client.scan(cursor=cursor, match=c['prefix'] + '*', count=100)
            if keys or cursor == 0:
                break
        return {'master': master, 'empty': not keys and cursor == 0}


async def http_probe(plan):
    from aiohttp import ClientSession, ClientTimeout, TCPConnector, DummyCookieJar
    context = ssl.create_default_context(cafile=plan['tls']['ca_bundle'])
    result = {}
    async with ClientSession(connector=TCPConnector(ssl=context), cookie_jar=DummyCookieJar(),
                             trust_env=False, timeout=ClientTimeout(total=3)) as client:
        for name, base, tls_name in (
                ('v1_health', plan['source']['api_origin'], None),
                ('alb_health', 'https://' + plan['target']['alb_dns_name'], plan['target']['tls_name'])):
            try:
                async with client.get(base + '/api/actuator/health', allow_redirects=False,
                    server_hostname=tls_name, headers={'Host': HOST}) as response:
                    result[name] = response.status == 200 and (await response.json()).get('status') == 'UP'
            except Exception:
                result[name] = False
    return result


def host_probes(plan, credentials=None):
    import asyncio
    result = {}
    try:
        result['source_host'] = metadata()
    except Exception:
        result['source_host'] = None
    # Refuse network probes on an unexpected host, not merely report it afterwards.
    if result['source_host'] != {'id': SOURCE, 'ip': plan['source']['private_ip']}:
        return result
    if not plan['handoff']['db_tests_complete']:
        return result
    result.update(asyncio.run(http_probe(plan)))
    for key, operation, name in [('mysql', mysql_probe, 'CUTOVER_MYSQL_PASSWORD'),
                                 ('redis', redis_probe, 'CUTOVER_REDIS_PASSWORD')]:
        if not credentials or not isinstance(credentials.get(name), str) or not credentials[name]:
            result[key] = {'error_type': 'MissingCredentials'}
            continue
        try:
            result[key] = operation(plan, credentials[name])
        except Exception as error:
            result[key] = {'error_type': type(error).__name__}  # never a raw driver/password message
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--plan', required=True)
    p.add_argument('--facts', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--host', action='store_true')
    p.add_argument('--credentials')
    args = p.parse_args()
    try:
        plan, facts = validate(read_json(args.plan)), read_json(args.facts)
        credentials = None
        if args.credentials:
            path = Path(args.credentials)
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise ValueError('credentials must be a private regular file')
            credentials = read_json(path)
        probes = host_probes(plan, credentials) if args.host else None
        report = evaluate(plan, facts, probes)
        private_file(args.output, json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
        if report['result'] != 'pass':
            raise SystemExit(2)
    except Exception as error:
        print(json.dumps({'result': 'failed', 'error_type': type(error).__name__}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
