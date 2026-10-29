"""Explicit V1 edge JWT profile. Does not install, reload or contact AWS."""
from nginx_user_read_profile import BACKEND_DIGEST, user_read_profile
from nginx_read_enforcement import ENFORCED_PREFIX, ENFORCED_SERVER
from nginx_create_enforcement import ENFORCED_LOCATION

JS_PATH = '/etc/yeodam/nginx-jwt-gate/gate.mjs'
CONFIG_PATH = '/etc/yeodam/nginx-jwt-gate/config.json'
PREFIX = '''# BEGIN yeodam staging JWT read gate
js_import yeodam_jwt from /etc/yeodam/nginx-jwt-gate/gate.mjs;
js_preload_object yeodam_jwt_config from /etc/yeodam/nginx-jwt-gate/config.json;
js_var $yeodam_jwt_user_key "";
js_var $yeodam_jwt_gate_result "not_applicable";
map "$request_method:$uri" $yeodam_jwt_read_candidate {
    default 0;
    "GET:/api/actuator/health" 0;
    "HEAD:/api/actuator/health" 0;
    "GET:/api/auth/csrf" 0;
    "HEAD:/api/auth/csrf" 0;
    "GET:/api/auth/kakao/authorize" 0;
    "HEAD:/api/auth/kakao/authorize" 0;
    "GET:/api/auth/kakao/callback" 0;
    "HEAD:/api/auth/kakao/callback" 0;
    ~^(GET|HEAD):/api/ 1;
}
# js_content can read a body; these read APIs do not accept one. Never buffer an attack body.
map "$yeodam_jwt_read_candidate:$http_content_length:$http_transfer_encoding" $yeodam_jwt_read_body {
    default 0;
    ~^1:(0)?:$ 0;
    ~^1: 1;
}
# Only server-computed, signature-verified identities enter this zone.
limit_req_zone $yeodam_jwt_user_key zone=yeodam_jwt_users:10m rate=5r/s;
# Separate budget: never runs on the signature-valid branch.
limit_req_zone $binary_remote_addr zone=yeodam_jwt_invalid:10m rate=25r/s;
# END yeodam staging JWT read gate

'''
ROUTING = '''        error_page 419 = @yeodam_jwt_gate;
        if ($yeodam_jwt_read_body) { return 413; }
        if ($yeodam_jwt_read_candidate) { return 419; }
'''
HEADERS = '''        proxy_hide_header X-Request-ID;
        add_header X-Request-ID $yeodam_staging_request_id always;
        add_header X-Edge-RateLimit-Scope "edge-user-read" always;
'''
LOCATIONS = '''
    # BEGIN yeodam staging JWT internal read routes
    location @yeodam_jwt_gate {
        js_content yeodam_jwt.gate;
        add_header X-Request-ID $yeodam_staging_request_id always;
    }
    location @yeodam_jwt_user_trips {
        limit_req zone=yeodam_jwt_users burst=30 nodelay;
        limit_req_status 429;
''' + HEADERS + '''        proxy_pass http://yeodam_backend;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
    location @yeodam_jwt_user_api {
        limit_req zone=yeodam_jwt_users burst=30 nodelay;
        limit_req_status 429;
''' + HEADERS + '''        proxy_pass http://yeodam_backend;
    }
    location @yeodam_jwt_reject {
        limit_req zone=yeodam_jwt_invalid burst=30 nodelay;
        limit_req_status 429;
        add_header X-Request-ID $yeodam_staging_request_id always;
        add_header X-Edge-RateLimit-Scope "edge-invalid-read" always;
        js_content yeodam_jwt.reject;
    }
    # END yeodam staging JWT internal read routes
'''


def instrument_jwt_gate(site, backend_digest):
    if backend_digest != BACKEND_DIGEST:
        raise ValueError('Reviewed V1 backend required')
    original_prefix = ENFORCED_PREFIX.replace('rate=5r/s;', 'rate=25r/s;')
    if (site.count(original_prefix) != 1 or site.count(ENFORCED_SERVER) != 1
            or site.count(ENFORCED_LOCATION) != 1 or 'yeodam_jwt_' in site
            or 'error_page 419' in site):
        raise ValueError('Expected the exact original V1 user-read profile')
    # Validate the entire pre-existing profile before touching it.
    legacy = site.replace(original_prefix, ENFORCED_PREFIX, 1)
    if user_read_profile(legacy, backend_digest) != site:
        raise ValueError('Unexpected V1 profile')
    site = site.replace(original_prefix, PREFIX, 1).replace(ENFORCED_SERVER, '', 1)
    for anchor in ['    location = /api/trips {\n', '    location /api/trips {\n', '    location /api/ {\n']:
        if site.count(anchor) != 1:
            raise ValueError('Ambiguous API routing')
        site = site.replace(anchor, anchor + ROUTING, 1)
    anchor = '    location / {\n        proxy_pass http://yeodam_frontend;'
    if site.count(anchor) != 1:
        raise ValueError('Unexpected frontend routing')
    site = site.replace(anchor, LOCATIONS + '\n' + anchor, 1)
    # The only new log values are finite result classes, never identities or tokens.
    field = "      '\"limit_req_status\":\"$limit_req_status\",'"
    if site.count(field) != 1:
        raise ValueError('Missing limiter observation field')
    return site.replace(field, field + "\n      '\"jwt_gate_result\":\"$yeodam_jwt_gate_result\",'", 1)
