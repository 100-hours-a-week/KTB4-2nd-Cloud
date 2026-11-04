"""Static BE runtime compatibility checks; do not read AWS, Kubernetes or Secrets."""
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / 'deploy/v3/apps/files/backend-contract.json').read_text())


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


def validate_json(text, mode):
    spring = structured_settings(text)
    require(set(spring) == {'app'}, 'SPRING_APPLICATION_JSON only carries reviewed non-secret app settings')
    app = spring['app']
    expected = {'trip', 'story', 'outbox'} | ({'attachment-conversion'} if mode == 'KAFKA' else set())
    require(set(app) == expected, 'Unreviewed/secret properties in structured app settings')
    groups = []
    for service in ('trip', 'story'):
        require(set(app[service]) == {'result-listener-enabled', 'result-consumer-group'}, 'Explicit result listener/group required')
        require(app[service]['result-listener-enabled'] is True, 'Full BE copies keep the same result listeners')
        groups.append(app[service]['result-consumer-group'])
    require(set(app['outbox']) == {'receipt-consumer-group'}, 'Explicit Outbox receipt group required')
    groups.append(app['outbox']['receipt-consumer-group'])
    require(all(isinstance(g, str) and g.startswith(CONTRACT['topicPrefix']) for g in groups), 'Do not join V2/default consumer groups')
    require(len(set(groups)) == len(groups), 'Different logical consumers need different groups')
    if mode == 'KAFKA':
        require(set(app['attachment-conversion']) == {'admission'}, 'Only conversion admission belongs in structured settings')
        admission = app['attachment-conversion']['admission']
        require(set(admission) == {'max-unfinished-photos', 'max-unfinished-bytes', 'max-unfinished-age',
                                   'snapshot-max-age', 'sample-interval', 'reopen-ratio'}, 'Complete admission policy required')
        for key in ('max-unfinished-photos', 'max-unfinished-bytes'):
            require(set(admission[key]) == {'LIGHT', 'HEIC'} and
                    all(type(v) is int and v > 0 for v in admission[key].values()), 'Positive per-lane admission budgets required')
        duration(admission['max-unfinished-age'])
        require(duration(admission['sample-interval']) <= duration(admission['snapshot-max-age']), 'Sampling interval exceeds observation lifetime')
        require(type(admission['reopen-ratio']) in (int, float) and 0 < admission['reopen-ratio'] < 1, 'Reopen ratio must be between zero and one')


def validate_backend(configs):
    common = None
    for config in configs.values():
        require(config['name'] == 'backend' and config['replicas'] == 1, 'Initial runtime profile is BE-only, one replica per copy')
        data = config.get('configMapData', {})
        require(not config['environment'] and not config['configMapRefs'], 'Runtime inputs must not shadow generated configuration')
        require(all(isinstance(data.get(k), str) and data[k].strip() for k in CONTRACT['requiredConfig'] + CONTRACT['topicConfig']),
                'Missing BE connection or runtime configuration')
        for key in CONTRACT['requiredSecrets']:
            require(config['secretRefs'].get(key) == {'name': 'backend-credentials', 'key': key}, 'Missing or incorrect BE Secret key reference')
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
        require(all(data[k].startswith(CONTRACT['topicPrefix']) for k in CONTRACT['topicConfig']), 'Use V3 AI topic names')
        require(len({data[k] for k in CONTRACT['topicConfig']}) == 6, 'Six distinct AI topics required')
        for name in ('startup', 'liveness', 'readiness'):
            path = '/actuator/health/readiness' if name == 'readiness' else '/actuator/health/liveness'
            require(config['probes'][name]['httpGet'] == {'path': path, 'port': 'management', 'scheme': 'HTTP'}, 'Incorrect BE Probe endpoint')
        require(data.get('SERVER_SHUTDOWN') == 'graceful', 'Use graceful Spring shutdown')
        grace = duration(data.get('SPRING_LIFECYCLE_TIMEOUT_PER_SHUTDOWN_PHASE', '0s'))
        require(config['terminationGracePeriodSeconds'] > config['preStopDelaySeconds'] + grace, 'Leave time for shutdown after preStop')
        mode = data['ATTACHMENT_CONVERSION_MODE']
        require(mode in ('DB', 'KAFKA'), 'Unsupported conversion mode')
        validate_json(data['SPRING_APPLICATION_JSON'], mode)
        require(data['KAFKA_TRIP_UPDATE_RESULT_CONSUMER_GROUP'].startswith(CONTRACT['topicPrefix']),
                'Trip update listener reads a direct environment Group; do not use its V2 default')
        app = structured_settings(data['SPRING_APPLICATION_JSON'])['app']
        require(data['KAFKA_TRIP_UPDATE_RESULT_CONSUMER_GROUP'] not in {
            app['trip']['result-consumer-group'], app['story']['result-consumer-group'], app['outbox']['receipt-consumer-group']},
                'Trip update must not reuse another logical consumer group')
        if mode == 'KAFKA':
            require(all(data.get(k, '').strip() for k in CONTRACT['conversionConfig'] + CONTRACT['conversionTopicConfig']), 'Missing conversion Kafka settings')
            require(all(data[k].startswith(CONTRACT['topicPrefix']) for k in CONTRACT['conversionTopicConfig'] + ['ATTACHMENT_CONVERSION_GROUP_PREFIX']), 'Separate V3 conversion topics and groups')
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
        identity = (data, config['secretRefs'])
        require(common is None or identity == common, 'Full BE copies must share functional settings and logical consumer groups')
        common = identity
