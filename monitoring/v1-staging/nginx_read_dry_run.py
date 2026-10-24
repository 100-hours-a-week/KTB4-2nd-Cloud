"""Staging-only read limit observation. No enforcement mode is exposed."""
import re

PREFIX = '''# BEGIN yeodam staging read dry run
map "$request_method:$uri" $yeodam_read_limit_key {
    default "";
    "GET:/api/actuator/health" "";
    "HEAD:/api/actuator/health" "";
    "GET:/api/auth/csrf" "";
    "HEAD:/api/auth/csrf" "";
    "GET:/api/auth/kakao/authorize" "";
    "HEAD:/api/auth/kakao/authorize" "";
    "GET:/api/auth/kakao/callback" "";
    "HEAD:/api/auth/kakao/callback" "";
    ~^(GET|HEAD):/api/ $binary_remote_addr;
}
limit_req_zone $yeodam_read_limit_key zone=yeodam_staging_reads:10m rate=5r/s;
# END yeodam staging read dry run

'''
SERVER = '''
    # Observe only; writes, auth exceptions and HTML have an empty key.
    limit_req zone=yeodam_staging_reads burst=30 nodelay;
    limit_req_dry_run on;
    limit_req_status 429;
'''


def instrument_read_dry_run(text):
    marker = '    server_name v1-staging.yeodam-2gether.com;'
    if PREFIX in text and text.count(SERVER) == 1:
        stripped = text.replace(PREFIX, '', 1).replace(SERVER, '', 1)
        if re.search(r'^\s*limit_(?:req|conn)(?:_\w+)?\s', stripped, re.M):
            raise ValueError('Other limit directives require explicit review')
        if text.count('"limit_req_status":"$limit_req_status"') == 1:
            return text
        raise ValueError('Partial dry-run logging configuration')
    if ('yeodam_staging_reads' in text or '$limit_req_status' in text
            or re.search(r'^\s*limit_(?:req|conn)(?:_\w+)?\s', text, re.M)):
        raise ValueError('Existing limit configuration requires explicit review')
    if text.count(marker) != 2:
        raise ValueError('Expected the exact V1 staging HTTP and HTTPS servers')
    position = text.rindex(marker)+len(marker)
    before = text[:position]
    if not re.search(r'listen\s+443\s+ssl', before[before.rindex('server {'):]):
        raise ValueError('Expected HTTPS server before insertion point')
    log = re.search(r'log_format\s+yeodam_json\s+escape=json\s+.*?;', text, re.S)
    if not log:
        raise ValueError('Expected JSON logging')
    line = next((line for line in log.group().splitlines() if '"request_id"' in line), None)
    if not line or not line.rstrip().endswith(",'"):
        raise ValueError('Unexpected request ID log field')
    result = text[:position]+SERVER+text[position:]
    result = result.replace(line, line+'\n      \'"limit_req_status":"$limit_req_status",\'', 1)
    return PREFIX+result
