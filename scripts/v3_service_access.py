"""Public Service identities and narrow QA-proven egress aliases; no live access."""
import ipaddress
import uuid

SERVICES = {
    'aiApi': {'namespace': 'ai', 'name': 'ai-api', 'port': 8000,
              'selector': {'app.kubernetes.io/instance': 'ai-api', 'yeodam.io/stage': 'v3'}},
    'tripApi': {'namespace': 'trip', 'name': 'trip-api', 'port': 8080,
                'selector': {'app.kubernetes.io/name': 'backend', 'app.kubernetes.io/instance': 'trip-api'}},
}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def address(value):
    require(isinstance(value, str), 'Service address must be an IPv4 string')
    ip = ipaddress.ip_address(value)
    network = ipaddress.ip_network('172.20.0.0/16')
    require(ip in network and ip not in (network.network_address, network.broadcast_address),
            'Exact foundation Service IPv4 address required')
    return str(ip)


def review(document):
    require(document.get('kind') in ('List', 'ServiceList') and len(document.get('items', [])) == 2,
            'Supply only ai/ai-api and trip/trip-api public Service objects')
    selected = {}
    for item in document['items']:
        require(item.get('apiVersion') == 'v1' and item.get('kind') == 'Service', 'Service objects only')
        meta = item.get('metadata', {})
        key = next((k for k, s in SERVICES.items()
                    if (meta.get('namespace'), meta.get('name')) == (s['namespace'], s['name'])), None)
        require(key is not None and key not in selected, 'Unexpected or duplicate Service identity')
        expected = SERVICES[key]
        uid = str(uuid.UUID(meta['uid']))
        spec = item['spec']
        require(spec.get('type', 'ClusterIP') == 'ClusterIP' and not spec.get('externalIPs') and
                not spec.get('externalName') and spec.get('selector') == expected['selector'],
                'Only the existing private app Service and exact Pod selector')
        ports = spec.get('ports', [])
        require(len(ports) == 1 and ports[0].get('port') == expected['port'] and
                ports[0].get('protocol', 'TCP') == 'TCP' and ports[0].get('targetPort') == 'http',
                'Service port must match the reviewed named app port')
        ip = address(spec['clusterIP'])
        require(spec.get('clusterIPs', [ip]) == [ip], 'Single IPv4 Service only')
        selected[key] = dict(expected, uid=uid, address=ip)
    require(set(selected) == set(SERVICES), 'Both private Services required')
    require(len({s['address'] for s in selected.values()}) == 2, 'Distinct Service addresses required')
    return {'version': 1, 'services': selected}


def values(evidence):
    return {'enabled': True, 'serviceAddresses': {k: s['address'] for k, s in evidence['services'].items()}}


def policies(addresses):
    require(set(addresses) == set(SERVICES), 'Exact two Service addresses required')
    ai, trip = address(addresses['aiApi']), address(addresses['tripApi'])
    require(ai != trip, 'Distinct Service addresses required')
    result = {}
    for namespace, roles in [('trip', ['trip-api', 'trip-result-worker']), ('photo', ['photo-api'])]:
        result[(namespace, 'allow-be-to-ai-service')] = {
            'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'backend', 'yeodam.io/stage': 'v3'},
                            'matchExpressions': [{'key': 'app.kubernetes.io/instance', 'operator': 'In', 'values': roles}]},
            'policyTypes': ['Egress'],
            'egress': [{'to': [{'ipBlock': {'cidr': ai + '/32'}}], 'ports': [{'protocol': 'TCP', 'port': 8000}]}],
        }
    result[('trip', 'allow-websocket-to-trip-service')] = {
        'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'websocket',
                                      'app.kubernetes.io/instance': 'trip-websocket', 'yeodam.io/stage': 'v3'}},
        'policyTypes': ['Egress'],
        'egress': [{'to': [{'ipBlock': {'cidr': trip + '/32'}}], 'ports': [{'protocol': 'TCP', 'port': 8080}]}],
    }
    return result
