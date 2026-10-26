"""Observe exact trip creation without turning the enforced read policy into Dry Run."""
from nginx_read_enforcement import instrument_read_enforcement

PREFIX = '''# BEGIN yeodam staging create dry run
map "$request_method:$uri" $yeodam_create_limit_key {
    default "";
    "POST:/api/trips" $binary_remote_addr;
}
limit_req_zone $yeodam_create_limit_key zone=yeodam_staging_creates:10m rate=1r/s;
# END yeodam staging create dry run

'''

LOCATION = '''    # BEGIN yeodam staging create observation route
    location = /api/trips {
        # Only return is used inside if. The named redirect preserves method/body/URI.
        error_page 418 = @yeodam_create_observe;
        if ($request_method = POST) { return 418; }
        # GET/HEAD inherit the existing enforced read policy.
        proxy_pass http://yeodam_backend;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }

    location @yeodam_create_observe {
        # Local limit_req replaces inherited read directives for this POST only.
        limit_req zone=yeodam_staging_creates burst=5 nodelay;
        limit_req_dry_run on;
        limit_req_status 429;
        proxy_pass http://yeodam_backend;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
    # END yeodam staging create observation route

'''
ANCHOR = '    location /api/trips {\n'


def instrument_create_dry_run(text):
    if PREFIX in text and LOCATION in text:
        if text.count(PREFIX) != 1 or text.count(LOCATION) != 1:
            raise ValueError('Duplicate create policy')
        original = text.replace(PREFIX, '', 1).replace(LOCATION, '', 1)
        if instrument_read_enforcement(original) != original:
            raise ValueError('Expected enforced reads')
        return text
    if ('yeodam_staging_creates' in text or 'yeodam_create_' in text
            or 'location = /api/trips' in text or 'error_page 418' in text):
        raise ValueError('Existing or partial create routing; inspect first')
    if text.count(ANCHOR) != 1 or instrument_read_enforcement(text) != text:
        raise ValueError('Expected exact staging source with enforced reads')
    return PREFIX + text.replace(ANCHOR, LOCATION + ANCHOR, 1)
