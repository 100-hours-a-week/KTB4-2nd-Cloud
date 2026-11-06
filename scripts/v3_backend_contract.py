"""Static BE runtime compatibility checks; do not read AWS, Kubernetes or Secrets."""
import json
from copy import deepcopy
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / 'deploy/v3/apps/files/backend-contract.json').read_text())
CONNECTION = json.loads((ROOT / 'deploy/v3/apps/files/connection-contract.json').read_text())
KAFKA_NAMES = json.loads((ROOT / 'deploy/v3/apps/files/business-contract.json').read_text())


def require(ok, message):
    if not ok:
        raise ValueError(message)


def host(value):
    require(value and value.lower() not in ('localhost', '0.0.0.0', '::1')
            and not value.startswith('127.'), 'Dependency must not point at the BE container itself')
    require(not any(c in value for c in ('/', '@', '?', ' ', '\n')), 'Use a hostname/address, not a credential-bearing URL')


def endpoint(value, schemes):
    parsed = urlsplit(value)
    require(parsed.scheme in schemes and parsed.hostname, 'Dependency URL has an unsupported or missing scheme/host')
    host(parsed.hostname)
    require(parsed.port is None or 0 < parsed.port < 65536, 'Dependency port is invalid')
    require(not parsed.username and not parsed.password, 'URL credentials belong in Secret references')
    require(not re.search(r'(password|secret|token|api[_-]?key)=', parsed.query, re.I), 'URL query must not carry credentials')
    return parsed


def duration(value):
    match = re.fullmatch(r'([0-9]+)(ms|s|m)', value)
    require(bool(match), 'Use explicit positive ms/s/m durations')
    result = int(match[1]) * {'ms': 0.001, 's': 1, 'm': 60}[match[2]]
    require(result > 0, 'Duration must be positive')
    return result


def structured_settings(text):
    def unique(pairs):
        values = {}
        for key, value in pairs:
            require(key not in values, 'Duplicate structured configuration key')
            values[key] = value
        return values
    return json.loads(text, object_pairs_hook=unique)


def prepare_config(config):
    """Mirror the Helm resolver; literal JSON input is never a second override path."""
    require('SPRING_APPLICATION_JSON' not in config['configMapData'],
            'Use springProperties instead of a raw JSON environment override')
    spring = config.get('springProperties')
    require(isinstance(spring, dict), 'springProperties must be structured')
    encoded = json.dumps(spring, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    config['configMapData']['SPRING_APPLICATION_JSON'] = encoded.replace('&', chr(92) + 'u0026').replace('<', chr(92) + 'u003c').replace('>', chr(92) + 'u003e')


def validate_json(text, role, connected=False):
    spring = structured_settings(text)
    require(isinstance(spring, dict) and set(spring) == ({'app', 'spring'} if connected else {'app'}), 'Only reviewed non-secret app settings belong in JSON')
    if connected:
        require(spring['spring'] == CONNECTION['spring'], 'Unreviewed TLS/connection/DB pool properties')
    app = spring['app']
    require(set(app) == {'trip', 'story', 'outbox', 'attachment-conversion'}, 'Unreviewed/secret structured properties')
    require(set(app['trip']) == {'result-listener-enabled', 'result-consumer-group'}, 'Explicit trip result listener/group required')
    require(app['trip']['result-listener-enabled'] is role['tripResultListener'], 'Trip result Listener must match role')
    # Story enablement is supplied by STORY_RESULT_LISTENER_ENABLED, never a higher-priority JSON override.
    require(set(app['story']) == {'result-consumer-group'}, 'Do not shadow the story environment flag in JSON')
    require(set(app['outbox']) == {'receipt-listener-enabled', 'receipt-consumer-group'}, 'Explicit receipt switch/group required')
    require(app['outbox']['receipt-listener-enabled'] is role['outboxReceiptListener'], 'Outbox receipt Listener must match role')
    groups = [app['trip']['result-consumer-group'], app['story']['result-consumer-group'], app['outbox']['receipt-consumer-group']]
    require(groups == [KAFKA_NAMES['backendSpringGroups'][k] for k in ('trip', 'story', 'outbox')],
            'Use exact BE handoff consumer groups; cluster endpoint isolates V3')
    require(len(set(groups)) == len(groups), 'Different logical consumers need different groups')
    conversion = app['attachment-conversion']
    require(set(conversion) == {'admission', 'operations'} and conversion['operations'] == {'enabled': False},
            'Only reviewed admission and disabled operations runner are permitted')
    require(type(conversion['operations']['enabled']) is bool, 'Operations flag must be a boolean')
    admission = conversion['admission']
    require(set(admission) == {'max-unfinished-photos', 'max-unfinished-bytes', 'max-unfinished-age',
                               'snapshot-max-age', 'sample-interval', 'reopen-ratio'}, 'Complete admission required on every role')
    for key in ('max-unfinished-photos', 'max-unfinished-bytes'):
        require(set(admission[key]) == {'LIGHT', 'HEIC'} and
                all(type(v) is int and v > 0 for v in admission[key].values()), 'Positive per-lane backlog budgets required')
    duration(admission['max-unfinished-age'])
    require(duration(admission['sample-interval']) <= duration(admission['snapshot-max-age']), 'Sampling exceeds observation lifetime')
    require(type(admission['reopen-ratio']) in (int, float) and 0 < admission['reopen-ratio'] < 1, 'Reopen ratio must be between zero and one')
    return spring


def validate_backend(configs, connected=False):
    common = None
    for instance, config in configs.items():
        require(config['name'] == 'backend' and config['replicas'] == 1, 'Initial runtime profile is BE-only, one replica per copy')
        data = config.get('configMapData', {})
        require(not config['environment'] and not config['configMapRefs'], 'Runtime inputs must not shadow generated configuration')
        require(all(isinstance(data.get(k), str) and data[k].strip() for k in CONTRACT['requiredConfig'] + CONTRACT['topicConfig']),
                'Missing BE connection or runtime configuration')
        allowed = set(CONTRACT['requiredConfig'] + CONTRACT['topicConfig'] + CONTRACT['conversionConfig'] + CONTRACT['conversionTopicConfig'] + CONTRACT['optionalConfig'] + ['SPRING_APPLICATION_JSON'])
        if connected:
            allowed |= set(CONNECTION['commonConfig']) | {'KAFKA_SASL_USERNAME'}
        require(set(data) <= allowed, 'Unreviewed ConfigMap key could bypass the role contract')
        require(data.get('JAVA_TOOL_OPTIONS') == (CONNECTION['javaToolOptions'] if connected else CONTRACT['javaToolOptions']), 'Use the reviewed heap settings; no JVM property override of role flags')
        require(set(config['secretRefs']) == set(CONTRACT['requiredSecrets']) | (set(CONNECTION['extraSecrets']) | {'KAFKA_SASL_PASSWORD'} if connected else set()), 'Only reviewed BE Secret bindings may inject environment values')
        for key in CONTRACT['requiredSecrets']:
            require(config['secretRefs'].get(key) == {'name': 'backend-credentials', 'key': key}, 'Missing or incorrect BE Secret key reference')
        if connected:
            require(all(config['secretRefs'].get(k) == {'name':'backend-credentials','key':k} for k in CONNECTION['extraSecrets']), 'Redis app/Sentinel credentials required')
            require(config['secretRefs']['KAFKA_SASL_PASSWORD'] == {'name':'kafka-'+instance,'key':'password'}, 'Kafka password must match role')
            require(data.get('KAFKA_SASL_USERNAME') == 'v3-'+instance, 'Kafka identity must match role')
            require(all(data.get(k)==v for k,v in CONNECTION['commonConfig'].items()), 'Connected V3 endpoint mismatch')
            require(config['secretVolumes'] == [{'name':'trust-input','secretName':'backend-trust','mountPath':'/etc/yeodam/ca'}], 'Exact public CA mount required')
        require('SPRING_MONGODB_URI' not in config['secretRefs'], 'Use one explicit MONGODB_URI binding')
        require(not {'KAFKA_TRIP_RESULT_LISTENER_ENABLED', 'KAFKA_TRIP_RESULT_CONSUMER_GROUP'}.intersection(data), 'Do not use local-only aliases in prod')
        require(instance in CONTRACT['workloadRoles'], 'Unknown BE role')
        role = CONTRACT['roleProfiles'][CONTRACT['workloadRoles'][instance]]
        require(all(data.get(key) == expected for key, expected in role['environment'].items()), 'Listener/polling flags differ from assigned role')
        require(data['SPRING_PROFILES_ACTIVE'] == 'prod,internal-metrics', 'Use the production profile and dedicated Probe profile')
        require(data['MANAGEMENT_SERVER_ADDRESS'] == '0.0.0.0' and config['ports'] == {'http': 8080, 'management': 9091}, 'Management listener must be reachable on its dedicated port')
        require(data['AWS_REGION'] == 'ap-northeast-2', 'Unexpected AWS region')
        require(re.fullmatch('[a-f0-9]{40}', data['RELEASE']), 'Record the selected BE source commit')
        require(data['MYSQL_URL'].startswith('jdbc:'), 'Use a MySQL JDBC URL')
        database = endpoint(data['MYSQL_URL'][5:], {'mysql'})
        require(database.path not in ('', '/'), 'Explicit MySQL database required')
        host(data['REDIS_HOST'])
        require(data['REDIS_PORT'].isdigit() and 0 < int(data['REDIS_PORT']) < 65536, 'Invalid Redis port')
        for url in ('AI_SERVER_BASE_URL', 'JWT_ISSUER', 'FRONTEND_ORIGIN', 'FRONTEND_OAUTH_CALLBACK_URI', 'KAKAO_REDIRECT_URI'):
            endpoint(data[url], {'http', 'https'})
        for address in data['KAFKA_BOOTSTRAP_SERVERS'].split(','):
            parsed = urlsplit('//' + address)
            host(parsed.hostname)
            require(parsed.port is not None and 0 < parsed.port < 65536, 'Kafka bootstrap port required')
        require(data['AUTH_SESSION_KEY_PREFIX'].startswith('yeodam:v3:foundation:')
                and data['WEBSOCKET_EVENTS_CHANNEL'].startswith('yeodam:v3:foundation:'), 'Separate V3 Redis keys and channels')
        require(all(data.get(k) == v for k, v in KAFKA_NAMES['backendEnvironment'].items()),
                'Topic and Group names must match the shared BE Kafka contract')
        require(len({data[k] for k in CONTRACT['topicConfig']}) == 6, 'Six distinct AI topics required')
        for name in ('startup', 'liveness', 'readiness'):
            path = '/actuator/health/readiness' if name == 'readiness' else '/actuator/health/liveness'
            require(config['probes'][name]['httpGet'] == {'path': path, 'port': 'management', 'scheme': 'HTTP'}, 'Incorrect BE Probe endpoint')
        require(data.get('SERVER_SHUTDOWN') == 'graceful', 'Use graceful Spring shutdown')
        grace = duration(data.get('SPRING_LIFECYCLE_TIMEOUT_PER_SHUTDOWN_PHASE', '0s'))
        require(config['terminationGracePeriodSeconds'] >= CONTRACT['minimumTerminationGraceSeconds'] and config['terminationGracePeriodSeconds'] > config['preStopDelaySeconds'] + grace + 60, 'Budget conversion drain plus shutdown after preStop')
        mode = data['ATTACHMENT_CONVERSION_MODE']
        require(mode == 'KAFKA', 'All roles retain KAFKA mode, including disabled consumers')
        validate_json(data['SPRING_APPLICATION_JSON'], role, connected)
        app = structured_settings(data['SPRING_APPLICATION_JSON'])['app']
        require(data['KAFKA_TRIP_UPDATE_RESULT_CONSUMER_GROUP'] not in {
            app['trip']['result-consumer-group'], app['story']['result-consumer-group'], app['outbox']['receipt-consumer-group']},
                'Trip update must not reuse another logical consumer group')
        require(all(data.get(k, '').strip() for k in CONTRACT['conversionConfig'] + CONTRACT['conversionTopicConfig']), 'Missing conversion Kafka settings')
        require(all(data[k] == KAFKA_NAMES['backendEnvironment'][k] for k in CONTRACT['conversionTopicConfig'] + ['ATTACHMENT_CONVERSION_GROUP_PREFIX']), 'Use BE handoff conversion names')
        topics = [data[k] for k in CONTRACT['conversionTopicConfig']]
        require(len(set(topics)) == 5, 'Four conversion topics and DLT must be distinct')
        require(not set(topics).intersection(data[k] for k in CONTRACT['topicConfig']), 'Do not reuse an AI topic for image conversion')
        for key in CONTRACT['conversionConfig']:
            if key.endswith('TOPIC') or key.endswith('PREFIX'):
                continue
            if key.endswith('BACKOFF') or key.endswith('INTERVAL'):
                duration(data[key])
            else:
                require(data[key].isdigit() and int(data[key]) > 0, 'Kafka count/byte settings must be positive integers')
        require(int(data['ATTACHMENT_CONVERSION_FETCH_MAX_BYTES']) >= int(data['ATTACHMENT_CONVERSION_PARTITION_FETCH_MAX_BYTES'])
                >= int(data['ATTACHMENT_CONVERSION_MAX_MESSAGE_BYTES']), 'Inconsistent conversion fetch/message budgets')
        common_data = {k: v for k, v in data.items() if k not in role['environment'] and k not in ('SPRING_APPLICATION_JSON', 'KAFKA_SASL_USERNAME')}
        shared_spring = deepcopy(app)
        del shared_spring['trip']['result-listener-enabled']
        del shared_spring['outbox']['receipt-listener-enabled']
        identity = (common_data, shared_spring, {k:v for k,v in config['secretRefs'].items() if k != 'KAFKA_SASL_PASSWORD'})
        require(common is None or identity == common, 'Common dependencies, budgets and logical groups must stay identical across roles')
        common = identity
