"""Add staging-only fields without rendering over rehearsal routing/headers."""
import re

CONNECTION_FIELDS = {
    'connection': '$connection',
    'connection_requests': '$connection_requests',
    'worker_pid': '$pid',
    'http_protocol': '$server_protocol',
}
MEASUREMENT_FIELDS = {'msec': '$msec', 'peer_addr': '$remote_addr'}
TIMING_FIELDS = {'upstream_header_time': '$upstream_header_time'}
FIELDS = {**CONNECTION_FIELDS, **MEASUREMENT_FIELDS, **TIMING_FIELDS}
SCRAPE = '''
prometheus.scrape "v1_nginx" {
  targets = [{__address__ = "127.0.0.1:9113", job = "v1-nginx", environment = "staging"}]
  scrape_interval = "15s"
  scrape_timeout = "5s"
  forward_to = [prometheus.remote_write.staging.receiver]
}
'''


def instrument_site(text):
    matches = list(re.finditer(r'log_format\s+yeodam_json\s+escape=json\s+.*?;', text, re.S))
    if len(matches) != 1:
        raise ValueError('Expected one yeodam_json log format')
    match = matches[0]
    block = match.group()
    quote = r'\"' if r'\"request_id\"' in block else '"'
    missing = {}
    for group in [CONNECTION_FIELDS, MEASUREMENT_FIELDS, TIMING_FIELDS]:
        present = [quote + key + quote in block for key in group]
        if any(present):
            if not all(quote + key + quote + ':' + quote + value + quote in block
                       for key, value in group.items()):
                raise ValueError('Partial or conflicting observability fields; inspect before rewriting')
        else:
            missing.update(group)
    if not missing:
        return text
    line = next((line for line in block.splitlines() if quote+'request_id'+quote in line), None)
    if line is None or not line.strip().startswith("'") or not line.rstrip().endswith(",'" ):
        raise ValueError('Unexpected request_id format')
    indent = line[:len(line)-len(line.lstrip())]
    extra = ''.join(f"\n{indent}'{quote}{key}{quote}:{quote}{value}{quote},'" for key, value in missing.items())
    updated = block.replace(line, line+extra, 1)
    return text[:match.start()]+updated+text[match.end():]


def instrument_alloy(text):
    if 'prometheus.scrape "v1_nginx"' in text:
        if SCRAPE.strip() not in text:
            raise ValueError('Existing v1_nginx scrape differs; inspect before rewriting')
        return text
    if 'prometheus.remote_write "staging"' not in text:
        raise ValueError('Expected existing staging remote write')
    return text.rstrip()+'\n'+SCRAPE
