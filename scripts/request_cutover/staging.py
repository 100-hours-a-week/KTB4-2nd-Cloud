"""Pinned existing-staging plan, local rendering and sanitized AWS describe calls."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import shutil
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qsl

from .config import private_file, run_id

ACCOUNT = '483175530259'
REGION = 'ap-northeast-2'
SOURCE = 'i-05d320e3a87200d5a'
VPC = 'vpc-0d84786e134e32933'
HOST = 'v1-staging.yeodam-2gether.com'
ALB = 'yeodam-v2-staging-alb-1866428186.ap-northeast-2.elb.amazonaws.com'
ROOT = Path(__file__).resolve().parents[2]
ASSETS = Path(__file__).resolve().parent / 'staging_assets'
if not ASSETS.exists():
    ASSETS = ROOT / 'deploy/request-cutover/staging'


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def read_json(path):
    return json.loads(Path(path).read_text(), object_pairs_hook=unique)


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate(plan):
    expected = read_json(ASSETS / 'plan.example.json')
    if set(plan) != set(expected) or any(set(plan[k]) != set(expected[k])
            for k in ('source', 'target', 'tls', 'mysql', 'redis', 'handoff')):
        raise ValueError('staging plan fields must match the example')
    if (type(plan['schemaVersion']) is not int or plan['schemaVersion'] != 1
            or plan['environment'] != 'staging-preparation'
            or plan['account_id'] != ACCOUNT or plan['region'] != REGION):
        raise ValueError('existing Seoul staging only')
    run_id(plan['run_id'])
    # Existing source, ingress and certificate locations only. This is not a general deployer.
    if plan['source'] != expected['source'] or plan['target'] != expected['target'] or plan['tls'] != expected['tls']:
        raise ValueError('unexpected source, public Host, ALB, TLS path or QA service')
    mysql = plan['mysql']
    for key in ('host', 'port', 'target_host', 'target_port', 'database', 'user', 'password_env'):
        if mysql[key] != expected['mysql'][key] or type(mysql[key]) is not type(expected['mysql'][key]):
            raise ValueError('unexpected MySQL route/schema; refuse QA or production')
    if mysql['expected_uuid'] is not None:
        run_id(mysql['expected_uuid'])
    if mysql['source_timezone'] not in ('UTC', 'Asia/Seoul'):
        raise ValueError('explicit source timezone required')
    for key in ('host', 'port', 'db', 'tls', 'password_env'):
        if plan['redis'][key] != expected['redis'][key] or type(plan['redis'][key]) is not type(expected['redis'][key]):
            raise ValueError('unexpected Redis endpoint, DB or TLS mode')
    if plan['redis']['prefix'] != 'yeodam:cutover:' + plan['run_id'] + ':':
        raise ValueError('run-specific inactive Redis prefix required')
    h = plan['handoff']
    if type(h['db_tests_complete']) is not bool:
        raise ValueError('db_tests_complete must be boolean')
    if h['evidence'] is not None and (not isinstance(h['evidence'], str)
            or not re.fullmatch(r'[A-Za-z0-9_./:#-]{1,300}', h['evidence'])):
        raise ValueError('use a non-secret evidence reference')
    if h['db_tests_complete'] and (not h['evidence'] or not mysql['expected_uuid']):
        raise ValueError('completed handoff requires evidence and current writer UUID')
    return plan


def gate_config(plan):
    return {
        'environment': 'staging-preparation', 'run_id': plan['run_id'],
        'source_instance_id': SOURCE, 'public_host': HOST,
        'source_timezone': plan['mysql']['source_timezone'],
        'v1_api': plan['source']['api_origin'], 'v1_web': plan['source']['web_origin'],
        'v2_origin': 'https://' + ALB, 'v2_tls_name': plan['target']['tls_name'],
        'upstream_ca_file': plan['tls']['ca_bundle'],
        'socket': '/run/yeodam-cutover/control.sock', 'journal': '/run/yeodam-cutover/journal.json',
        'hold_seconds': 5, 'max_waiters': 1000, 'max_wait_bytes': 67108864,
        'mysql': {k: plan['mysql'][k] for k in ('host', 'port', 'database', 'user', 'password_env')},
        'redis': plan['redis'], 'preparation_only': True,
    }


def validate_gate(config):
    if (config.get('preparation_only') is not True or config.get('source_instance_id') != SOURCE
            or config['public_host'] != HOST or config['v1_api'] != 'http://127.0.0.1:8080'
            or config['v1_web'] != 'http://127.0.0.1:3000' or config['v2_origin'] != 'https://' + ALB
            or config.get('v2_tls_name') != 'staging.yeodam-2gether.com'
            or config['mysql']['database'] != 'yeodam_v1_source'
            or config['mysql']['host'] != '127.0.0.1' or config['mysql'].get('port') != 16033
            or config['mysql']['user'] != 'yeodam_app'):
        raise ValueError('invalid staging preparation runtime')
    # This profile is deliberately passthrough-only; it cannot start HOLD/import.


def validate_a_gate(config):
    """Constrain the live A rehearsal to the existing Source and isolated target."""
    expected = read_json(ASSETS / 'plan.example.json')
    if (config.get('environment') != 'staging-a-rehearsal'
            or config.get('preparation_only', False)
            or config.get('require_seal') is not True
            or config.get('source_instance_id') != SOURCE
            or config.get('public_host') != HOST
            or config.get('v1_api') != 'http://127.0.0.1:8080'
            or config.get('v1_web') != 'http://127.0.0.1:3000'
            or config.get('v2_origin') != 'https://' + ALB
            or config.get('v2_tls_name') != 'staging.yeodam-2gether.com'
            or config.get('v2_route_header') != config.get('run_id')
            or config.get('a_db_target') != expected['mysql']['target_host']
            or config.get('a_db_journal') != '/var/lib/yeodam/migration-cutover/active.json'
            or config.get('mysql') != {k: expected['mysql'][k] for k in ('host', 'port', 'database', 'user', 'password_env')}
            or config.get('redis') != {**{k: expected['redis'][k] for k in ('host', 'port', 'db', 'tls', 'password_env')},
                                       'prefix': 'yeodam:cutover:' + config['run_id'] + ':'}
            or config.get('hold_seconds') != 5):
        raise ValueError('invalid A staging rehearsal scope')
    for key in ('socket', 'journal'):
        if not str(config.get(key, '')).startswith('/run/yeodam-cutover/'):
            raise ValueError('A rehearsal requires host-mounted private gate paths')
    try:
        client = ipaddress.IPv4Address(config['v2_client_ip'])
    except (KeyError, ipaddress.AddressValueError) as error:
        raise ValueError('A rehearsal requires an exact V2 task IP') from error
    if client not in ipaddress.IPv4Network('10.30.10.0/24') or str(client) == expected['mysql']['target_host']:
        raise ValueError('V2 task must be separate from the migration DB')


def render(plan, release, output):
    validate(plan)
    if not re.fullmatch('[0-9a-f]{40}', release):
        raise ValueError('full Cloud commit required')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    values = {'PUBLIC_HOST': HOST, **{k.upper(): v for k, v in plan['tls'].items()},
              'RUN_ID': plan['run_id'], 'RELEASE': release}
    template = (ASSETS / 'nginx.conf.tmpl').read_text()
    nginx = re.sub(r'@@([A-Z_]+)@@', lambda match: values[match[1]], template)
    private_file(output / 'plan.json', json.dumps(plan, indent=2) + '\n')
    private_file(output / 'gate.json', json.dumps(gate_config(plan), indent=2) + '\n')
    private_file(output / 'nginx.candidate.conf', nginx)
    private_file(output / 'credentials.example.json', json.dumps({
        'CUTOVER_MYSQL_PASSWORD': None, 'CUTOVER_REDIS_PASSWORD': None}) + '\n')
    for name in ('compose.yml', 'operate.sh'):
        private_file(output / name, (ASSETS / name).read_text())
    shutil.copytree(Path(__file__).resolve().parent, output / 'request_cutover',
                    ignore=shutil.ignore_patterns('__pycache__', 'staging_assets'))
    shutil.copytree(ASSETS, output / 'request_cutover/staging_assets')
    requirements = ROOT / 'deploy/request-cutover/requirements.txt'
    private_file(output / 'requirements.txt', requirements.read_text())
    dockerfile = (ROOT / 'deploy/request-cutover/Dockerfile').read_text().replace(
        'deploy/request-cutover/requirements.txt', 'requirements.txt').replace(
        'scripts/request_cutover', 'request_cutover')
    dockerfile = dockerfile.replace('WORKDIR /opt/yeodam',
        'WORKDIR /opt/yeodam\nLABEL org.opencontainers.image.revision=' + release)
    private_file(output / 'Dockerfile', dockerfile)
    private_file(output / '.dockerignore', '**\n!Dockerfile\n!requirements.txt\n!request_cutover/\n!request_cutover/**\n')
    files = {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(output.rglob('*')) if path.is_file()}
    private_file(output / 'bundle.json', json.dumps({'run_id': plan['run_id'], 'release': release,
        'plan_hash': signature(plan), 'environment': plan['environment'], 'files': files}, indent=2) + '\n')
    return {'run_id': plan['run_id'], 'plan_hash': signature(plan), 'rendered': True,
            'handoff_complete': plan['handoff']['db_tests_complete'], 'deployed': False}


def public_environment(values):
    allowed = {'MYSQL_URL', 'JWT_ISSUER', 'FRONTEND_ORIGIN', 'FRONTEND_OAUTH_CALLBACK_URI',
        'KAKAO_REDIRECT_URI', 'REDIS_HOST', 'REDIS_PORT', 'REDIS_SSL_ENABLED', 'AUTH_SESSION_KEY_PREFIX'}
    result = {v['name']: v['value'] for v in values if v['name'] in allowed}
    # Some JDBC URLs inline credentials. Do not save those even as diagnostics.
    if 'MYSQL_URL' in result:
        try:
            url = urlsplit(result['MYSQL_URL'].removeprefix('jdbc:'))
            query_keys = {key for key, _ in parse_qsl(url.query)}
            if url.username or url.password or query_keys - {'serverTimezone', 'characterEncoding', 'useSSL', 'sslMode', 'allowPublicKeyRetrieval'}:
                result['MYSQL_URL'] = '<redacted-unsupported-mysql-url>'
        except ValueError:
            result['MYSQL_URL'] = '<redacted-invalid-mysql-url>'
    return result


def aws_facts(plan, profile):
    """Only fixed describe/list/get identity APIs; no SSM, secret reads or mutations."""
    validate(plan)
    def aws(*args):
        raw = subprocess.check_output(['aws', '--profile', profile, '--region', REGION,
            '--cli-connect-timeout', '3', '--cli-read-timeout', '10', '--no-cli-pager',
            *args, '--output', 'json'], stderr=subprocess.PIPE)
        return json.loads(raw)
    if aws('sts', 'get-caller-identity')['Account'] != ACCOUNT:
        raise ValueError('wrong AWS account')
    instance = aws('ec2', 'describe-instances', '--instance-ids', SOURCE)['Reservations'][0]['Instances'][0]
    services = aws('ecs', 'describe-services', '--cluster', plan['target']['cluster'],
                   '--services', plan['target']['service'])['services']
    if len(services) != 1:
        raise ValueError('expected exactly one target service')
    service = services[0]
    definition = aws('ecs', 'describe-task-definition', '--task-definition', service['taskDefinition'])['taskDefinition']
    backend = next(c for c in definition['containerDefinitions'] if c['name'] == 'backend')
    alb = aws('elbv2', 'describe-load-balancers', '--names', 'yeodam-v2-staging-alb')['LoadBalancers'][0]
    listeners = aws('elbv2', 'describe-listeners', '--load-balancer-arn', alb['LoadBalancerArn'])['Listeners']
    https = next(l for l in listeners if l['Port'] == 443 and l['Protocol'] == 'HTTPS')
    names = []
    for cert in https.get('Certificates', []):
        names.extend(aws('acm', 'describe-certificate', '--certificate-arn', cert['CertificateArn'])['Certificate']['SubjectAlternativeNames'])
    health = []
    for target in service.get('loadBalancers', []):
        health.extend(t['TargetHealth']['State'] for t in aws('elbv2', 'describe-target-health',
            '--target-group-arn', target['targetGroupArn'])['TargetHealthDescriptions'])
    return {'run_id': plan['run_id'], 'plan_hash': signature(plan),
        'observed_at': datetime.now(timezone.utc).isoformat(), 'account_id': ACCOUNT,
        'source': {'id': instance['InstanceId'], 'ip': instance.get('PrivateIpAddress'),
                   'vpc': instance['VpcId'], 'state': instance['State']['Name']},
        'alb': {'dns': alb['DNSName'], 'vpc': alb['VpcId'], 'certificate_names': names},
        'backend': {'service': service['serviceName'], 'definition': service['taskDefinition'],
                    'running': service['runningCount'], 'desired': service['desiredCount'],
                    'target_health': health, 'image': backend['image'],
                    'env': public_environment(backend.get('environment', []))}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('render', 'inventory'))
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--release')
    parser.add_argument('--profile', default='yeodam-admin')
    args = parser.parse_args()
    try:
        plan = validate(read_json(args.plan))
        if args.command == 'render':
            result = render(plan, args.release or '', args.output)
        else:
            result = aws_facts(plan, args.profile)
            private_file(args.output, json.dumps(result, indent=2) + '\n')
            result = {'inventory_saved': True, 'run_id': plan['run_id']}
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({'result': 'failed', 'error_type': type(error).__name__,
                          'message': str(error) if type(error) is ValueError else 'inspect private diagnostics'}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
