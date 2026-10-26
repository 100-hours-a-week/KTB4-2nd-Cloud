"""Switch only the reviewed creation policy; keep reads, proxy settings and burst unchanged."""
from nginx_create_dry_run import LOCATION, instrument_create_dry_run

LEGACY_ENFORCED_LOCATION = LOCATION.replace('limit_req_dry_run on;', 'limit_req_dry_run off;')
ENFORCED_LOCATION = LEGACY_ENFORCED_LOCATION.replace('        limit_req_status 429;', '''        limit_req_status 429;
        # Return the same ID for BE responses and Nginx-generated rejections.
        proxy_hide_header X-Request-ID;
        add_header X-Request-ID $yeodam_staging_request_id always;''')


def instrument_create_observation(text):
    for block in [ENFORCED_LOCATION, LEGACY_ENFORCED_LOCATION]:
        if block in text:
            if text.count(block) != 1:
                raise ValueError('Duplicate enforced create route')
            text = text.replace(block, LOCATION, 1)
            break
    return instrument_create_dry_run(text)


def instrument_create_enforcement(text):
    return instrument_create_observation(text).replace(LOCATION, ENFORCED_LOCATION, 1)
