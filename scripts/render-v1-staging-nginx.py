#!/usr/bin/env python3
"""Render staging Nginx and preserve valid probe request IDs through the ingress."""
import argparse
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "monitoring/v1-staging"))
from nginx_observability import instrument_site
from nginx_read_enforcement import instrument_read_enforcement
from nginx_keepalive import instrument_keepalive
from nginx_create_enforcement import instrument_create_enforcement
from nginx_user_read_profile import user_read_profile

PROBE_ROUTE = '/api/internal/migration-write-probe'

def render(template, release, *, user_read_enabled=False, backend_digest=None):
    if not re.fullmatch('[0-9a-f]{40}', release):
        raise ValueError('Cloud release must be a full commit SHA')
    marker = 'map "$request_method:$uri" $yeodam_normalized_route {'
    if marker not in template or 'proxy_set_header X-Request-ID $request_id;' not in template:
        raise ValueError('Unexpected Nginx template; refuse a partial staging rewrite')
    content = template.replace('yeodam-2gether.com', 'v1-staging.yeodam-2gether.com').replace('__YEODAM_RELEASE__', release)
    content = content.replace('$request_id', '$yeodam_staging_request_id')
    content = content.replace("'\"method\":\"$request_method\",'", "'\"migration_run_id\":\"$yeodam_staging_probe_run_id\",'\n      '\"method\":\"$request_method\",'")
    content = content.replace(marker, marker + f'\n    "POST:{PROBE_ROUTE}" "{PROBE_ROUTE}";')
    # All ordinary traffic retains the Nginx-generated ID. Only the staging
    # probe's valid canonical UUID is carried into BE and the access log.
    prefix = '''map "$request_method:$uri:$http_x_request_id" $yeodam_staging_request_id {
    default $request_id;
    "~^POST:/api/internal/migration-write-probe:(?<yeodam_valid_probe_request_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$" $yeodam_valid_probe_request_id;
}

map "$request_method:$uri:$http_x_migration_run_id" $yeodam_staging_probe_run_id {
    default "";
    "~^POST:/api/internal/migration-write-probe:(?<yeodam_valid_probe_run_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$" $yeodam_valid_probe_run_id;
}

'''
    # #424: verified idle cleanup and authenticated reads plus real150-photo completion.
    site = instrument_create_enforcement(instrument_keepalive(instrument_read_enforcement(instrument_site(prefix + content)), 15))
    if user_read_enabled:
        return user_read_profile(site, backend_digest)
    if backend_digest:
        raise ValueError('Backend digest requires explicit user-limiter activation attestation')
    return site

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--release', required=True)
    parser.add_argument('--user-read-limit-enabled', action='store_true')
    parser.add_argument('--backend-digest')
    args = parser.parse_args()
    args.output.write_text(render(args.template.read_text(), args.release,
                                  user_read_enabled=args.user_read_limit_enabled,
                                  backend_digest=args.backend_digest))
